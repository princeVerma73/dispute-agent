from langchain_core.tools import tool

@tool
def check_refund_policy(trip_status: str, distance_km: float, fare: float) -> str:
    """
    Always use this tool BEFORE approving any refund to check the company rules.
    Pass the trip's status, distance in km, and original fare into this tool.
    """
    
    # Rule 1: Completed trips
    status_lower = trip_status.lower()
    if status_lower == "completed":
        return "Policy: Trip was completed successfully. Strictly NO REFUND allowed."
    
    # Rule 2: Cancelled or failed trips
    if any(k in status_lower for k in ["failed", "cancelled", "cancel"]):
        if distance_km < 5.0:
            return f"Policy: Short trip ({distance_km}km) issue. APPROVED for 100% refund of ₹{fare}."
        else:
            partial_refund = round(fare * 0.5, 2)
            return f"Policy: Long trip issue. APPROVED for 50% partial refund of ₹{partial_refund}."
            
    return "Policy: Unclear scenario. Tell the rider that manual review is required."