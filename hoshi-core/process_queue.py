#!/usr/bin/env python3
"""Считает pending-задачи в очереди."""
from storage import list_queue_items
from config import INBOX

if __name__ == "__main__":
    print(len(list_queue_items(INBOX, status="pending")))
