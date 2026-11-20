def process(payload):
    changes = (10, 7000, 9800, 9899)
    for index in range(9900):
        candidate = index if index in changes else payload
        payload = candidate
        if index == 9899:
            print("finished", payload)


if __name__ == "__main__":
    process(-1)
