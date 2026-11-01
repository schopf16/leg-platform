"""The account models, split by topic; every public name is re-exported here (migrations, settings
and other apps import ``leg_platform.accounts.models.X``)."""

# ruff: noqa: F401
from leg_platform.accounts.models.common import (
    EMAIL_MAX_LENGTH,
    EXACT_COLLATION,
    SWISS_POSTAL_CODE,
    SWISS_POSTAL_CODE_MESSAGE,
    SWITZERLAND,
    AccountIntegrityError,
    CustomerNumberUnavailableError,
    parse_swiss_postal_code,
    validate_account_email,
    validate_hex_colour,
    validate_https_address,
    validate_instance_name,
)
from leg_platform.accounts.models.customer_numbers import (
    CUSTOMER_NUMBER_ATTEMPTS,
    CUSTOMER_NUMBER_DIGITS,
    CUSTOMER_NUMBER_SPACE,
    CUSTOMER_NUMBER_WARNING_PERCENT,
    IssuedCustomerNumber,
    IssuedCustomerNumberManager,
    IssuedCustomerNumberQuerySet,
    customer_numbers_nearly_exhausted,
    format_customer_number,
)
from leg_platform.accounts.models.user import User
from leg_platform.accounts.models.log_notice import LogNotificationSettings
from leg_platform.accounts.models.passkeys import Passkey, PasskeyChallenge, PasskeyIdentity, PendingSignInChange
from leg_platform.accounts.models.user_manager import (
    SUPERUSER_LOCK_TIMEOUT,
    UserManager,
    UserQuerySet,
)
from leg_platform.accounts.models.security import (
    ConfirmationFailure,
    LoginIpAttempt,
    LoginIpBucket,
    RateLimitBucket,
    RateLimitEvent,
    SecurityEvent,
    SecurityEventManager,
    SecurityEventQuerySet,
    SessionCodeFailure,
)
from leg_platform.accounts.models.tokens import (
    VERIFICATION_TOKEN_BYTES,
    VERIFICATION_TOKEN_LENGTH,
    VERIFICATION_TOKEN_PURPOSE,
    PendingEmailChange,
    VerificationToken,
    VerificationTokenManager,
    verification_token_digest,
)
from leg_platform.accounts.models.registration_codes import (
    CHALLENGE_LENGTH,
    CODE_DIGITS,
    CODE_PURPOSE,
    RegistrationChallenge,
    challenge_digest,
    code_digest,
    new_code,
)
from leg_platform.accounts.models.mail import (
    CERTIFICATE_EXPIRED,
    CERTIFICATE_EXPIRING,
    CERTIFICATE_NONE,
    CERTIFICATE_VALID,
    GRAPH_CERTIFICATE_KEY_PURPOSE,
    GRAPH_CERTIFICATE_WARNING_DAYS,
    GRAPH_MIN_RSA_KEY_BITS,
    GREETING_NAME_MAX_LENGTH,
    EmailText,
    EmailTextManager,
    EmailTextVersion,
    GraphConfiguration,
    MailJob,
)
from leg_platform.accounts.models.portal import HiddenTask, PortalSettings
from leg_platform.accounts.models.second_factor import (
    AUTHENTICATOR_CODE,
    AUTHENTICATOR_KEY_BYTES,
    AUTHENTICATOR_SECRET_PURPOSE,
    RECOVERY_CODE_PURPOSE,
    AuthenticatorDevice,
    AuthenticatorDeviceManager,
    RecoveryCode,
    RecoveryCodeDevice,
    recovery_code_digest,
)
