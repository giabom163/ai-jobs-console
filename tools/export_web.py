#!/usr/bin/env python
"""Compatibility CLI: use the canonical console export and discovery-time contract.

The historical posted_at/open-only blog export is retired. Consumers receive
exactly the canonical console payload, including explicit review statuses.
"""
if __package__:
    from .export_console import connect, build as build_payload, main
else:
    from export_console import connect, build as build_payload, main

if __name__ == '__main__':
    main()
