"""WebAuthn ceremonies verified by py_webauthn 3.0.1 (D-106).

Lock order: account, challenge, credential, failure counters. A failed verification consumes
its challenge too. Synced credentials are supported; physical device independence is not proven.
"""

import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import Passkey, PasskeyChallenge, PasskeyIdentity, PortalSettings, User

LIFETIME = timedelta(minutes=5)
SESSION_KEY = "passkey_binding"
MAX_CREDENTIALS = 20
MAX_CREDENTIAL_BYTES = 1024


def relying_party():
    """Canonical configuration only, never Host, Origin or any other request header."""
    parsed = urlsplit(PortalSettings.instance_origin())
    if (
        not parsed.hostname
        or parsed.username or parsed.password
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname == "localhost"))
    ):
        raise ImproperlyConfigured(
            "Passkeys require an HTTPS address of the portal in the portal settings "
            "(http://localhost is allowed locally)."
        )
    return parsed.hostname, f"{parsed.scheme}://{parsed.netloc}"


def binding(request):
    nonce = request.session.setdefault(SESSION_KEY, secrets.token_urlsafe(32))
    return keyed_digest(nonce, b"LEG-Platform WebAuthn session v1")


def credential_digest(raw_id):
    return hashlib.sha256(raw_id).hexdigest()


def credential_from_response(response):
    """Reject malformed, ambiguous and unreasonably large credential ids before a DB lookup."""
    if (
        not isinstance(response, dict) or response.get("type") != "public-key"
        or not isinstance(response.get("rawId"), str)
        or not isinstance(response.get("id"), str)
    ):
        raise ValueError("Invalid credential.")
    raw_id = base64url_to_bytes(response["rawId"])
    if (
        not raw_id or len(raw_id) > MAX_CREDENTIAL_BYTES
        or response.get("id") != bytes_to_base64url(raw_id)
    ):
        raise ValueError("Invalid credential id.")
    return raw_id


def _client_context(response):
    """No iframe/cross-origin ceremony, including unexpected topOrigin (W3C WebAuthn)."""
    data = response.get("response")
    if not isinstance(data, dict) or not isinstance(data.get("clientDataJSON"), str):
        raise ValueError("Invalid client data.")
    client = json.loads(base64url_to_bytes(data["clientDataJSON"]))
    if not isinstance(client, dict) or client.get("crossOrigin", False) is not False or "topOrigin" in client:
        raise ValueError("Cross-origin ceremonies are refused.")


def _may_hold_credential(user, registering):
    """An active account, or (``registering``) the unverified account of a registration whose link is in the session
    (D-106): the caller has checked the link; the key stays inactive until the last step of the registration."""
    return user.is_active or (registering and user.status == User.Status.UNVERIFIED)


def begin(request, purpose, *, user=None, action="", mode=None, registering=False):
    """Create bounded, short-lived options. No credential ids are disclosed for public login."""
    if purpose not in {"login", "register", "proof"} or len(action) > 200:
        raise ValueError("Invalid ceremony purpose.")
    if registering and purpose != "register":
        raise ValueError("Only a registration ceremony can run before the account is active.")
    if purpose == "register":
        if not isinstance(mode, str) or mode not in {"this_device", "another_device"}:
            raise ValueError("Invalid registration mode.")
    elif mode is not None:
        raise ValueError("Registration mode is not valid for authentication.")
    if purpose != "login" and (user is None or not _may_hold_credential(user, registering)):
        raise ValueError("An active account is required.")
    rp_id, _origin = relying_party()
    with transaction.atomic():
        if user is not None:
            user = User._base_manager.select_for_update().get(pk=user.pk)
            if not _may_hold_credential(user, registering):
                raise ValueError("An active account is required.")
        session_binding = binding(request)
        # Repeated clicks replace this session's options; expired rows are opportunistically removed.
        PasskeyChallenge.objects.filter(expires_at__lte=timezone.now()).delete()
        PasskeyChallenge.objects.filter(binding=session_binding, purpose=purpose).delete()
        if purpose == "register":
            user.passkeys.filter(active=False, created_at__lte=timezone.now() - LIFETIME).delete()
            if user.passkeys.count() >= MAX_CREDENTIALS:
                raise ValueError("Credential limit reached.")
            identity, _ = PasskeyIdentity.objects.get_or_create(user=user)
            options = generate_registration_options(
                rp_id=rp_id, rp_name=settings.INSTANCE_NAME,
                user_name=user.email, user_id=bytes(identity.handle),
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=(
                        AuthenticatorAttachment.CROSS_PLATFORM if mode == "another_device"
                        else AuthenticatorAttachment.PLATFORM
                    ),
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                # W3C Level 3: pair HYBRID with cross-platform for older user agents.
                # This guides the native prompt, not proof of a physical device's identity.
                hints=[
                    PublicKeyCredentialHint.HYBRID if mode == "another_device"
                    else PublicKeyCredentialHint.CLIENT_DEVICE
                ],
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=bytes(key.credential_id))
                    for key in user.passkeys.all()
                ],
            )
        else:
            options = generate_authentication_options(
                rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
                allow_credentials=[
                    PublicKeyCredentialDescriptor(id=bytes(key.credential_id))
                    for key in user.passkeys.filter(active=True)
                ] if purpose == "proof" else [],
            )
        row = PasskeyChallenge.objects.create(
            user=user, purpose=purpose, action=action, binding=session_binding,
            auth_hash=user.get_session_auth_hash() if user else "",
            challenge=options.challenge, expires_at=timezone.now() + LIFETIME,
        )
    return {"ceremony": str(row.pk), "options": json.loads(options_to_json(options))}


def _take(request, ceremony, purpose, user, action):
    """Caller holds the account lock. Deletion survives validation failure."""
    try:
        row = PasskeyChallenge.objects.select_for_update().filter(pk=ceremony).first()
    except (ValueError, TypeError, ValidationError):
        return None
    if row is None:
        return None
    # A different session must not consume the owner's challenge.
    if not constant_time_compare(row.binding, binding(request)):
        return None
    row.delete()
    if (
        row.purpose != purpose or row.action != action or row.expires_at <= timezone.now()
        or row.user_id != (user.pk if purpose != "login" and user else None)
        or (row.auth_hash and (user is None or not constant_time_compare(row.auth_hash, user.get_session_auth_hash())))
    ):
        return None
    return row


def register(request, ceremony, response, name, *, user=None, registering=False):
    """Return a staged verified credential, or None. Activation is a separate atomic switch.

    ``user`` and ``registering``: the unverified account of a registration (the caller has checked the link in the
    session); the key stays inactive until ``User.complete_registration`` switches it on in the last step.
    """
    rp_id, origin = relying_party()
    with transaction.atomic():
        user = User._base_manager.select_for_update().get(pk=(user or request.user).pk)
        row = _take(request, ceremony, "register", user, "")
        if row is None or not _may_hold_credential(user, registering) or user.passkeys.count() >= MAX_CREDENTIALS:
            return None
        try:
            raw_id = credential_from_response(response)
            _client_context(response)
            verified = verify_registration_response(
                credential=response, expected_challenge=bytes(row.challenge),
                expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True,
            )
            if raw_id != verified.credential_id:
                return None
            with transaction.atomic():
                return Passkey.objects.create(
                    user=user, credential_digest=credential_digest(raw_id), credential_id=raw_id,
                    public_key=verified.credential_public_key, sign_count=verified.sign_count,
                    name=name, setup_binding=binding(request),
                    backup_eligible=verified.credential_device_type.value == "multi_device",
                    backed_up=verified.credential_backed_up,
                )
        except (WebAuthnException, ValueError, TypeError, KeyError, IntegrityError):
            return None


def authenticate(request, ceremony, response, *, user=None, action=""):
    """Verify a login or a current-proof assertion; caller integrates its failure budget."""
    purpose = "proof" if user is not None else "login"
    try:
        raw_id = credential_from_response(response)
        key = Passkey.objects.filter(credential_digest=credential_digest(raw_id), active=True).first()
    except (ValueError, TypeError, KeyError):
        key = None
    # Unknown credentials still consume a matching public challenge.
    with transaction.atomic():
        target = User.objects.select_for_update().filter(pk=user.pk if user else (key.user_id if key else None)).first()
        row = _take(request, ceremony, purpose, target, action)
        if (
            row is None or key is None or target is None or not target.is_active
            or target.authentication_method != User.AuthenticationMethod.PASSKEY
            or key.user_id != target.pk
        ):
            return None
        key = Passkey.objects.select_for_update().filter(pk=key.pk, active=True).first()
        if key is None:
            return None
        try:
            rp_id, origin = relying_party()
            _client_context(response)
            raw_handle = response["response"].get("userHandle")
            if not isinstance(raw_handle, str):
                return None
            handle = base64url_to_bytes(raw_handle)
            if not constant_time_compare(handle, bytes(target.passkey_identity.handle)):
                return None
            verified = verify_authentication_response(
                credential=response, expected_challenge=bytes(row.challenge),
                expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=bytes(key.public_key),
                credential_current_sign_count=key.sign_count, require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError, PasskeyIdentity.DoesNotExist):
            return None
        key.sign_count = verified.new_sign_count
        key.last_used_at = timezone.now()
        key.backed_up = verified.credential_backed_up
        key.save(update_fields=["sign_count", "last_used_at", "backed_up"])
        return target
