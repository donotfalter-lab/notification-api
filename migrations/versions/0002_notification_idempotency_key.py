"""add notifications.idempotency_key

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("idempotency_key", sa.String(255), nullable=True))
    op.create_index(
        "ix_notifications_tenant_id_idempotency_key",
        "notifications",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_tenant_id_idempotency_key", table_name="notifications")
    op.drop_column("notifications", "idempotency_key")
