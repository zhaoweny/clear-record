<%
def literal(value):
    """A revision identifier as a double-quoted literal, or None."""
    return f'"{value}"' if value else "None"
%>\
"""${message}

Revision ID: ${up_revision}
% if down_revision:
Revises: ${down_revision | comma,n}
% else:
Revises:
% endif
Create Date: ${create_date}

"""

from __future__ import annotations
% if upgrades:

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
% endif

# revision identifiers, used by Alembic.
revision: str = "${up_revision}"
down_revision: str | None = ${literal(down_revision)}
branch_labels: str | None = ${literal(branch_labels)}
depends_on: str | None = ${literal(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
