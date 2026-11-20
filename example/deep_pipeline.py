import sys

from pipeline import process


def forward(envelope, remaining):
    if remaining == 0:
        cleaned = envelope.strip()
        return process(payload=cleaned)
    next_depth = remaining - 1
    return relay(envelope, next_depth)


def relay(message, remaining):
    next_depth = remaining - 1
    return forward(message, next_depth)


def main():
    sys.setrecursionlimit(5000)
    original = "  parcel-1000  "
    return forward(original, 1000)


if __name__ == "__main__":
    main()
