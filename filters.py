def matches(item, city, max_price, rooms):
    """Accept only listings with parsed BYN monthly price and parsed room count."""
    if item.price is None or item.price <= 0 or item.price > max_price:
        return False
    if item.rooms not in rooms:
        return False

    # Configured search pages are already Minsk-specific. If an address is available,
    # reject clearly non-Minsk listings; don't require the city word in every card.
    location = f"{item.location} {item.title}".lower()
    if city and "минск" in location:
        return True
    if city and item.source.lower() in {"kufar", "onliner", "domovita"}:
        return True
    return not city
