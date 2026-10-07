"""Coordinate child creation and unit archive in this application's process.

The app serves a single session manager. Hold this lock from worker creation
through run registration, or from archive validation through commit. Never
await external work while holding it. The database transaction is the durable
boundary; the lock prevents a child launch passing validation in its gap.
"""
from threading import RLock

unit_lock = RLock()
