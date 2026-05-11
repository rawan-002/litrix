from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views, sync_views, invitation_views

urlpatterns = [
    path('departments-public/',      views.public_departments,        name='public-departments'),
    path('register/',                views.register,                  name='register'),
    path('registration-match/',      views.registration_match,        name='registration-match'),
    path('verify-email/',            views.verify_email,              name='verify-email'),
    path('login/',                   views.login,                     name='login'),
    path('logout/',                  views.logout,                    name='logout'),
    path('refresh/',                 TokenRefreshView.as_view(),      name='refresh'),
    path('me/',                      views.me,                        name='me'),
    path('change-password/',         views.change_password,           name='change-password'),
    path('password-reset/',          views.password_reset_request,    name='password-reset'),
    path('password-reset/confirm/',  views.password_reset_confirm,    name='password-reset-confirm'),

    path('registrations/',                            views.list_pending_registrations, name='registrations-list'),
    path('registrations/<int:request_id>/approve/',   views.approve_registration,       name='registrations-approve'),
    path('registrations/<int:request_id>/reject/',    views.reject_registration,        name='registrations-reject'),

    path('users/',                                    views.list_users,                 name='users-list'),
    path('users/<int:user_id>/',                      views.update_user,                name='users-update'),
    path('roles/',                                    views.list_roles,                 name='roles-list'),
    path('roles/create/',                             views.create_role,                name='roles-create'),
    path('roles/<int:role_id>/',                      views.delete_role,                name='roles-delete'),
    path('roles/<int:role_id>/permissions/',          views.get_role_permissions,       name='role-perms-get'),
    path('roles/<int:role_id>/set-permissions/',      views.set_role_permissions,       name='role-perms-set'),
    path('permissions/',                              views.list_permissions,           name='permissions-list'),

    path('notifications/',                            views.list_notifications,         name='notifications-list'),
    path('notifications/<int:notif_id>/read/',        views.mark_notification_read,     name='notifications-read'),
    path('notifications/mark-all-read/',              views.mark_all_read,              name='notifications-mark-all'),

    path('audit-log/',                                views.list_audit_log,             name='audit-log'),

    path('sync/researchers/',                         sync_views.list_syncable_researchers, name='sync-researchers'),
    path('sync/jobs/',                                sync_views.list_sync_jobs,        name='sync-jobs'),
    path('sync/trigger/',                             sync_views.trigger_sync,          name='sync-trigger'),

    # Role-scoped invitations
    path('invitations/',                              invitation_views.list_or_create_invitation, name='invitations-list-create'),
    path('invitations/<int:invitation_id>/',          invitation_views.revoke_invitation,         name='invitations-revoke'),
    path('invitations/lookup/<str:token>/',           invitation_views.get_invitation,            name='invitations-lookup'),
]
