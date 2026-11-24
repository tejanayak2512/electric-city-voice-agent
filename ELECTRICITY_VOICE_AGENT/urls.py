"""
URL configuration for ELECTRICITY_VOICE_AGENT project.
"""
from django.contrib import admin
from django.urls import path
from . import views as mainviews
from admins import department_views as deptviews
from admins import views as adminviews
from complaints import views as complaintviews
from users import views as userviews
from voice_agent import views as voiceviews

urlpatterns = [
    path('admin/', admin.site.urls),
    # projects views urls
    path("", mainviews.basefucntion, name="home"),

    # admin side views urls mapping
    path("admin-login/", adminviews.admin_login, name="admin_login"),
    path("admins/adminhome/", adminviews.admin_home, name="admin_home"),
    path("admins/transformers/add/", adminviews.add_transformer, name="add_transformer"),
    path("admins/transformers/<int:tf_id>/delete/", adminviews.delete_transformer, name="delete_transformer"),
    path("admins/power-supply/", adminviews.toggle_power_supply, name="toggle_power_supply"),
    path("admins/maintenance/", adminviews.admin_maintenance, name="admin_maintenance"),
    path("admins/maintenance/<int:sched_id>/process/", adminviews.admin_process_maintenance, name="admin_process_maintenance"),
    path("admins/employees/", adminviews.manage_employees, name="admin_employees"),
    path("admins/employees/add/", adminviews.add_employee_view, name="admin_add_employee"),
    path("admins/employees/<int:emp_id>/toggle/", adminviews.toggle_employee_status, name="toggle_employee_status"),
    path("admins/employees/<int:emp_id>/delete/", adminviews.delete_employee, name="delete_employee"),
    path("admins/connections/", adminviews.manage_connections, name="admin_connections"),
    path("admins/connections/issue-meters/", adminviews.connections_issue_meters_view, name="admin_connections_issue_meters"),
    path("admins/connections/power-controls/", adminviews.connections_power_controls_view, name="admin_connections_power_controls"),
    path("admins/connections/ledger/", adminviews.connections_ledger_view, name="admin_connections_ledger"),
    path("admins/connections/<int:app_id>/verify-payment/", adminviews.admin_verify_payment, name="admin_verify_payment"),
    path("admins/connections/<int:app_id>/process/", adminviews.admin_process_connection, name="admin_process_connection"),
    path("admins/connections/<int:app_id>/power-cut/", adminviews.toggle_connection_power_cut, name="toggle_connection_power_cut"),
    path("admins/billing/", adminviews.admin_billing, name="admin_billing"),
    path("admins/billing/commercial/", adminviews.admin_billing_commercial, name="admin_billing_commercial"),
    path("admins/billing/tariffs/", adminviews.admin_billing_tariffs, name="admin_billing_tariffs"),
    path("admins/revenue/", adminviews.admin_revenue, name="admin_revenue"),
    path("admins/settings/", adminviews.admin_settings, name="admin_settings"),
    path("admins/settings/news/", adminviews.admin_settings_news, name="admin_settings_news"),
    path("admins/settings/notifications/", adminviews.admin_settings_notifications, name="admin_settings_notifications"),
    path("admins/settings/media/", adminviews.admin_settings_media, name="admin_settings_media"),
    path("admins/settings/llm/", adminviews.admin_settings_llm, name="admin_settings_llm"),
    path("admins/settings/llm/<int:config_id>/test/", adminviews.admin_settings_llm_test_api, name="admin_settings_llm_test_api"),
    path("admins/voice-agents/", adminviews.admin_voice_agents, name="admin_voice_agents"),
    path("admins/voice-agents/<int:agent_id>/toggle/", adminviews.admin_toggle_agent_status, name="admin_toggle_agent_status"),
    path("admins/voice-agents/<int:agent_id>/sample-voice/", adminviews.admin_agent_sample_voice, name="admin_agent_sample_voice"),
    path("power-status/", adminviews.power_status_api, name="power_status_api"),
    path("admin-logout/", adminviews.admin_logout, name="admin_logout"),

    # department side views urls mapping
    path("department-login/", deptviews.department_login, name="department_login"),
    path("department-login/verify-id/", deptviews.department_verify_employee_id, name="department_verify_employee_id"),
    path("department/home/", deptviews.department_home, name="department_home"),
    path("department/verify-requests/", deptviews.department_verify_requests, name="department_verify_requests"),
    path("department/install-meters/", deptviews.department_install_meters, name="department_install_meters"),
    path("department/substations/", deptviews.department_substations, name="department_substations"),
    path("department/connected-premises/", deptviews.department_connected_premises, name="department_connected_premises"),
    path("department/maintenance/", deptviews.department_maintenance, name="department_maintenance"),
    path("department/maintenance/<int:sched_id>/process/", deptviews.department_process_maintenance, name="department_process_maintenance"),
    path("department/live-usage/", deptviews.department_live_usage_api, name="department_live_usage_api"),
    path("department/connections/<int:app_id>/verify-payment/", deptviews.department_verify_payment, name="department_verify_payment"),
    path("department/connections/<int:app_id>/verify/", deptviews.department_verify_connection, name="department_verify_connection"),
    path("department/connections/<int:app_id>/connect/", deptviews.department_connect_household, name="department_connect_household"),
    path("department-logout/", deptviews.department_logout, name="department_logout"),

    # user side views urls mapping
    path("create-account/", userviews.create_account, name="create_account"),
    path("user-login/", userviews.user_login, name="user_login"),
    path("users/userhome/", userviews.user_home, name="user_home"),
    path("users/services/", userviews.user_services, name="user_services"),
    path("users/services/new-connection/", userviews.user_new_connection, name="user_new_connection"),
    path("users/services/new-connection/pay/", userviews.user_pay_connection_fee, name="user_pay_connection_fee"),
    path("users/services/new-connection/<int:app_id>/pdf/", userviews.download_connection_pdf, name="download_connection_pdf"),
    path("users/services/connection-status/", userviews.user_connection_status, name="user_connection_status"),
    path("users/services/power-simulation/", userviews.user_power_simulation, name="user_power_simulation"),
    path("users/services/account-profile/", userviews.user_account_profile, name="user_account_profile"),
    path("users/services/billing/", userviews.user_billing, name="user_billing"),
    path("users/services/billing/<int:bill_id>/invoice-pdf/", userviews.download_bill_invoice_pdf, name="download_bill_invoice_pdf"),
    path("users/services/billing/pay/", userviews.user_pay_bill, name="user_pay_bill"),
    path("users/apply-connection/", userviews.apply_connection, name="apply_connection"),
    path("users/house-power/", userviews.toggle_house_power, name="toggle_house_power"),
    path("users/usage/record/", userviews.record_power_usage_view, name="record_power_usage"),
    path("users/appliances/add/", userviews.add_appliance, name="add_appliance"),
    path("users/appliances/builtin/<str:app_key>/toggle/", userviews.toggle_builtin_appliance, name="toggle_builtin_appliance"),
    path("users/appliances/<int:app_id>/toggle/", userviews.toggle_appliance_state, name="toggle_appliance_state"),
    path("users/appliances/<int:app_id>/delete/", userviews.delete_appliance, name="delete_appliance"),
    path("admins/complaints/", complaintviews.admin_complaints_list, name="admin_complaints"),
    path("department/complaints/", complaintviews.department_complaints_list, name="department_complaints"),
    path("department/complaints/<int:comp_id>/process/", complaintviews.department_process_complaint, name="department_process_complaint"),

    # AI Voice Agent (Calling Agent & Speech Telephony API)
    path("users/services/voice-agent/", voiceviews.user_voice_agent_call, name="user_voice_agent"),
    path("users/services/voice-calling/", voiceviews.user_pure_voice_agent_call, name="user_pure_voice_agent"),
    path("api/voice-agent/start-call/", voiceviews.api_start_call, name="api_voice_agent_start_call"),
    path("api/voice-agent/message/", voiceviews.api_send_message, name="api_voice_agent_message"),
    path("api/voice-agent/end-call/", voiceviews.api_end_call, name="api_voice_agent_end_call"),
    path("api/voice-agent/track/<str:query>/", voiceviews.api_track_complaint, name="api_voice_agent_track"),

    path("user-logout/", userviews.user_logout, name="user_logout"),
]

from django.conf import settings
from django.conf.urls.static import static

if settings.MEDIA_ROOT:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

