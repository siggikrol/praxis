def split_hosts(endpoints):
    internal, external = [], []

    def is_internal(h):
        return ".int." in h

    for ep in endpoints:
        # Normalize hosts from textarea string -> list of hostnames
        raw = ep.get("hosts", "")

        if isinstance(raw, str):
            hosts = [h.strip() for h in raw.splitlines() if h.strip()]
        else:
            hosts = list(raw or [])

        # Split into internal/external host lists
        i = [h for h in hosts if is_internal(h)]
        e = [h for h in hosts if not is_internal(h)]

        if i:
            internal.append({**ep, "hosts": i})
        if e:
            external.append({**ep, "hosts": e})

    return internal, external
