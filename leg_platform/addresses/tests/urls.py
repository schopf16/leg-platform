from leg_platform import urls as project_urls

# The administration pages link into the portal (the start page, the management area: D-113, ``admin/base_site.html``),
# so this configuration is the project's own routes, plus nothing the project does not have. Everything an admin page
# resolves with ``{% url %}`` exists here; a new hard dependency of the admin shell cannot break these tests again.
urlpatterns = list(project_urls.urlpatterns)
