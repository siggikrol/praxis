def process_form_data(name: str, count: int, mode: str) -> dict:
    """Return a dictionary based on validated form data."""
    return {
        "name": name,
        "count": count,
        "mode": mode,
        "status": "processed"
    }
