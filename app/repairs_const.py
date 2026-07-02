STATUS_LABELS = {
    "RECEIVED": "Received",
    "DIAGNOSED": "Diagnosed",
    "WAITING": "Waiting for Parts",
    "IN_PROGRESS": "In Progress",
    "READY": "Ready for Pickup",
    "COMPLETED": "Completed",
    "COLLECTED": "Collected",
}
STATUS_ORDER = ["RECEIVED", "DIAGNOSED", "WAITING", "IN_PROGRESS", "READY", "COMPLETED", "COLLECTED"]
STATUS_BADGE = {
    "RECEIVED": "badge-gray", "DIAGNOSED": "badge-blue",
    "WAITING": "badge-amber", "IN_PROGRESS": "badge-purple",
    "READY": "badge-green", "COMPLETED": "badge-green", "COLLECTED": "badge-gray",
}
ISSUE_TYPES = [
    "Screen Replacement", "Battery Replacement", "Charging Port",
    "Camera Repair", "Speaker / Mic", "Water Damage", "Software Issue", "Other",
]


def next_status(current: str):
    if current not in STATUS_ORDER:
        return None
    idx = STATUS_ORDER.index(current)
    return STATUS_ORDER[idx + 1] if idx + 1 < len(STATUS_ORDER) else None
