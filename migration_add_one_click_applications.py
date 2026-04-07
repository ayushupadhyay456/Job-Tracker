"""add ats improvement fields to users

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6   ← put your actual current head here (flask db current)
Create Date: 2026-04-05

USAGE:
    flask db upgrade      # adds 4 columns to the users table
    flask db downgrade    # removes them cleanly
"""

from alembic import op
import sqlalchemy as sa


revision      = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'   # ← replace with your actual current head revision
branch_labels = None
depends_on    = None


def upgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column(
            'ats_improved_text',
            sa.Text(),
            nullable=True,
            comment='AI-rewritten resume text after ATS optimisation',
        ))
        batch_op.add_column(sa.Column(
            'ats_original_score',
            sa.Integer(),
            nullable=True,
            comment='Cosine similarity score BEFORE ATS improvement',
        ))
        batch_op.add_column(sa.Column(
            'ats_improved_score',
            sa.Integer(),
            nullable=True,
            comment='Cosine similarity score AFTER ATS improvement',
        ))
        batch_op.add_column(sa.Column(
            'ats_improved_at',
            sa.DateTime(),
            nullable=True,
            comment='Timestamp of ATS improvement; NULL means not yet used for this resume',
        ))


def downgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('ats_improved_at')
        batch_op.drop_column('ats_improved_score')
        batch_op.drop_column('ats_original_score')
        batch_op.drop_column('ats_improved_text')