"""Database connection for race-day-sim."""

import os

import psycopg2
import psycopg2.extras


def get_connection():
    host = os.environ.get("SIM_DB_HOST", "localhost")
    port = os.environ.get("SIM_DB_PORT", "5432")
    name = os.environ.get("SIM_DB_NAME", "handycapper")
    user = os.environ.get("SIM_DB_USER", "handycapper")
    password = os.environ.get("SIM_DB_PASSWORD", "handycapper")
    return psycopg2.connect(
        host=host, port=port, dbname=name, user=user, password=password
    )
