#!/usr/bin/env python3
"""Hybrid retrieve: dense cosine + BM25 fused by RRF, with provenance."""
import argparse
import json

from brain_retrieval import BrainRetriever


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--q", required=True)
    parser.add_argument("--k", type=positive_int, default=5)
    args = parser.parse_args()
    payload = BrainRetriever(args.index).query(args.q, args.k)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
