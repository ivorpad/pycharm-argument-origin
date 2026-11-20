def process(payload: str):
    print("received", payload)
    payload = payload.upper()
    print("sent", payload)
    return payload


def normalize(incoming):
    cleaned = incoming.strip()
    return process(payload=cleaned)


def dispatch(envelope):
    return normalize(envelope)


def main():
    source = "  parcel-17  "
    return dispatch(source)


if __name__ == "__main__":
    main()
