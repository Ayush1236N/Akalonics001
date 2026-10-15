"""forecast_rows table — Demand Forecasting (T7) historical demand data

Revision ID: c2f7a1b9d4e0
Revises: b1e9e59df68d
Create Date: 2026-09-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c2f7a1b9d4e0'
down_revision = 'b1e9e59df68d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('forecast_rows',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('owner_id', sa.String(length=32), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('date', sa.DateTime(timezone=False), nullable=False),
        sa.Column('sku', sa.String(length=120), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('forecast_rows', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_forecast_rows_owner_id'), ['owner_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_forecast_rows_category'), ['category'], unique=False)
        batch_op.create_index(batch_op.f('ix_forecast_rows_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_forecast_rows_sku'), ['sku'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('forecast_rows', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_forecast_rows_sku'))
        batch_op.drop_index(batch_op.f('ix_forecast_rows_date'))
        batch_op.drop_index(batch_op.f('ix_forecast_rows_category'))
        batch_op.drop_index(batch_op.f('ix_forecast_rows_owner_id'))
    op.drop_table('forecast_rows')
