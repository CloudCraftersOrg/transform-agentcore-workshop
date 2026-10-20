"""Tables (rendered by scaffold_from_pattern from the legacy schema)."""

from sqlalchemy import Column, Integer, MetaData, Table

metadata = MetaData()

example = Table("example", metadata, Column("id", Integer, primary_key=True))
