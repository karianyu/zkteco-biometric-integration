import frappe
from frappe.model.document import Document
from zkteco_biometric_integration.zkteco_biometric_integration.utils import (
    make_http_request,
    update_integration_request_log,
    map_checkin,
)
from frappe.utils import get_datetime
from frappe.integrations.utils import create_request_log


@frappe.whitelist()
def handle_employee_checkin(start_time=None):
    biometric_settings = frappe.get_all(
        "ZKTeco Biometric Settings", filters={"is_fetch_enabled": 1}
    )

    for setting in biometric_settings:
        # Isolate each device: a failure on one must not stop the others.
        try:
            sync_device_checkins(setting.name, start_time)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                title="ZKTeco Checkin Sync Failed",
                message=f"Device setting: {setting.name}\n\n{frappe.get_traceback()}",
            )


def sync_device_checkins(setting_name: str, start_time=None) -> None:
    setting_doc = frappe.get_doc("ZKTeco Biometric Settings", setting_name)

    ensure_valid_token(setting_doc)

    # Upper bound is captured once and becomes the new watermark only after the
    # whole window is processed successfully, so a crash mid-run never advances
    # past unprocessed punches. Overlapping windows are safe (checkins dedupe).
    end_time = get_datetime()
    window_start = start_time or setting_doc.last_fetched_time
    # Accept either a datetime or a string watermark.
    if window_start:
        window_start = get_datetime(window_start)

    transactions = get_transactions(setting_doc, window_start, end_time)

    for txn in transactions:
        if emp_checkin := create_employee_checkin(txn):
            if setting_doc.enable_mandatory_checkin:
                manage_user(emp_checkin)

    frappe.db.set_value(
        "ZKTeco Biometric Settings",
        setting_doc.name,
        "last_fetched_time",
        end_time,
    )
    frappe.db.commit()


def ensure_valid_token(setting_doc: Document) -> None:
    """Refresh the JWT in isolation so an auth hiccup can't kill the whole run."""
    if not setting_doc.is_token_expired():
        return
    try:
        setting_doc.generate_token()
        frappe.db.set_value(
            "ZKTeco Biometric Settings",
            setting_doc.name,
            {
                "token": setting_doc.token,
                "issued_at": setting_doc.issued_at,
                "expiry": setting_doc.expiry,
            },
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            title="ZKTeco Token Refresh Failed", message=frappe.get_traceback()
        )


def get_transactions(setting_doc: Document, start_time=None, end_time=None) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"JWT {setting_doc.token}",
    }

    url = f"{setting_doc.url}/iclock/api/transactions/"
    end_time = end_time or get_datetime()

    params = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else None,
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "page_size": 2500,
        "page": 1,
    }

    integration_request_log = create_request_log(
        data=params,
        integration_type="Remote",
        service_name="ZKTeco Biometric Integration",
        request_headers=headers,
        request_url=url,
        reference_doctype="ZKTeco Biometric Settings",
        reference_docname=setting_doc.name,
    )

    transactions: list[dict] = []
    try:
        # Walk every page; BioTime caps a response and returns `next` when more
        # rows exist. Without this, anything past the first page is silently lost.
        while True:
            response = make_http_request(
                method="GET", url=url, headers=headers, params=params
            )

            if not response or not response.get("data"):
                break

            transactions.extend(response["data"])

            if not response.get("next"):
                break
            params["page"] += 1

        update_integration_request_log(
            integration_request_log,
            status="Completed",
            response={"fetched": len(transactions)},
        )
        return transactions
    except Exception as e:
        update_integration_request_log(
            integration_request_log, status="Failed", error=str(e)
        )
        frappe.log_error(frappe.get_traceback(), str(e))
        # Re-raise so the caller does NOT advance the watermark on a failed fetch.
        raise


def create_employee_checkin(transaction: dict) -> None:
    employee = frappe.db.get_value(
        "Employee", {"attendance_device_id": transaction.get("emp_code")}, "name"
    )
    if not employee:
        # Unmapped device id — skip rather than inserting an orphan checkin.
        return

    log_type = map_checkin(transaction.get("punch_state_display"))
    punch_time = transaction.get("punch_time")

    if frappe.db.exists(
        "Employee Checkin",
        {"employee": employee, "time": punch_time, "log_type": log_type},
    ):
        return

    try:
        frappe.set_user("ZKTeco Biometric")
        employee_checkin = frappe.get_doc(
            {
                "doctype": "Employee Checkin",
                "employee": employee,
                "time": punch_time,
                "log_type": log_type,
                "device_id": transaction.get("terminal_sn"),
            }
        )
        employee_checkin.insert(ignore_permissions=True)
        return employee_checkin

    except Exception:
        frappe.log_error(
            title="Employee Checkin Creation Error", message=frappe.get_traceback()
        )
    finally:
        frappe.set_user("Guest")


def activate_user(user_id: str, log_type: str) -> None:
    try:
        if "System Manager" in frappe.get_roles(user_id):
            return

        should_enable = log_type == "IN"

        frappe.db.set_value(
            "User", user_id, "enabled", int(should_enable), update_modified=False
        )

    except Exception:
        frappe.log_error(message=frappe.get_traceback(), title="User Activation Error")


def manage_user(employee_checkin: Document):
    if frappe.db.exists("Employee", employee_checkin.employee):
        employee = frappe.get_doc("Employee", employee_checkin.employee)

        if employee.user_id:
            activate_user(employee.user_id, employee_checkin.log_type)
