# ===== order_utils.py =====
import logging

def update_order_status(order_id, status):
    logging.info(f"[ORDER] {order_id} updated to {status}")
    # implement broker status update logic here

def map_status_code(code):
    mapping = {
        1605: "Subscribed",
        1606: "Unsubscribed",
        # add more codes as needed
    }
    return mapping.get(code, f"Unknown({code})")