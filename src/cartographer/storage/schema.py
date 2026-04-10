"""LadybugDB schema definitions for the graph-context model.

Three layers:
  Layer 1 (Structure): Files, Modules, Classes, Functions, Types, Variables,
                        Endpoints, Events, Schemas + all structural/data-flow edges
  Layer 2 (History):    Commits, Changes + history edges
  Layer 3 (Planning):   Plans, Intents + planning edges
"""

# ---------------------------------------------------------------------------
# Layer 1: Structure
# ---------------------------------------------------------------------------

STRUCTURE_NODE_TABLES = [
    """CREATE NODE TABLE IF NOT EXISTS File (
        path STRING,
        lang STRING,
        hash STRING,
        last_modified STRING,
        PRIMARY KEY (path)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Module (
        path STRING,
        name STRING,
        PRIMARY KEY (path)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Class (
        id STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        visibility STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Function (
        id STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        signature STRING,
        visibility STRING,
        is_method BOOL,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Type (
        id STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Variable (
        id STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Endpoint (
        id STRING,
        path STRING,
        method STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Event (
        id STRING,
        name STRING,
        channel STRING,
        file_path STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Schema (
        id STRING,
        name STRING,
        file_path STRING,
        line_start INT64,
        line_end INT64,
        store_type STRING,
        PRIMARY KEY (id)
    )""",
]

STRUCTURE_REL_TABLES = [
    # Structural
    "CREATE REL TABLE IF NOT EXISTS IMPORTS (FROM File TO File)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_FUNC (FROM File TO Function)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_CLASS (FROM File TO Class)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_TYPE (FROM File TO Type)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_VAR (FROM File TO Variable)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_ENDPOINT (FROM File TO Endpoint)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_SCHEMA (FROM File TO Schema)",
    "CREATE REL TABLE IF NOT EXISTS HAS_METHOD (FROM Class TO Function)",
    "CREATE REL TABLE IF NOT EXISTS CALLS (FROM Function TO Function)",
    "CREATE REL TABLE IF NOT EXISTS INHERITS (FROM Class TO Class)",
    "CREATE REL TABLE IF NOT EXISTS BELONGS_TO (FROM File TO Module)",
    "CREATE REL TABLE IF NOT EXISTS DEPENDS_ON (FROM Module TO Module)",
    # Data flow — function I/O
    "CREATE REL TABLE IF NOT EXISTS EXPECTS_TYPE (FROM Function TO Type)",
    "CREATE REL TABLE IF NOT EXISTS EXPECTS_CLASS (FROM Function TO Class)",
    "CREATE REL TABLE IF NOT EXISTS RETURNS_TYPE (FROM Function TO Type)",
    "CREATE REL TABLE IF NOT EXISTS RETURNS_CLASS (FROM Function TO Class)",
    "CREATE REL TABLE IF NOT EXISTS YIELDS_TYPE (FROM Function TO Type)",
    "CREATE REL TABLE IF NOT EXISTS YIELDS_CLASS (FROM Function TO Class)",
    "CREATE REL TABLE IF NOT EXISTS USES_TYPE (FROM Function TO Type)",
    "CREATE REL TABLE IF NOT EXISTS USES_CLASS (FROM Function TO Class)",
    # Data flow — shared state
    "CREATE REL TABLE IF NOT EXISTS READS (FROM Function TO Variable)",
    "CREATE REL TABLE IF NOT EXISTS WRITES (FROM Function TO Variable)",
    # Data flow — events
    "CREATE REL TABLE IF NOT EXISTS EMITS (FROM Function TO Event)",
    "CREATE REL TABLE IF NOT EXISTS HANDLES (FROM Function TO Event)",
    # Data flow — persistence
    "CREATE REL TABLE IF NOT EXISTS READS_FROM (FROM Function TO Schema)",
    "CREATE REL TABLE IF NOT EXISTS WRITES_TO (FROM Function TO Schema)",
    "CREATE REL TABLE IF NOT EXISTS MAPS_TO_SCHEMA (FROM Class TO Schema)",
    # API boundaries
    "CREATE REL TABLE IF NOT EXISTS EXPOSES (FROM Module TO Endpoint)",
    "CREATE REL TABLE IF NOT EXISTS ROUTE_HANDLER (FROM Endpoint TO Function)",
    "CREATE REL TABLE IF NOT EXISTS MIDDLEWARE (FROM Endpoint TO Function)",
]

# ---------------------------------------------------------------------------
# Layer 2: History
# ---------------------------------------------------------------------------

HISTORY_NODE_TABLES = [
    """CREATE NODE TABLE IF NOT EXISTS Commit (
        hash STRING,
        message STRING,
        author STRING,
        timestamp STRING,
        PRIMARY KEY (hash)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Change (
        id STRING,
        file_path STRING,
        additions INT64,
        deletions INT64,
        change_type STRING,
        PRIMARY KEY (id)
    )""",
]

HISTORY_REL_TABLES = [
    "CREATE REL TABLE IF NOT EXISTS CHANGED_IN (FROM File TO Commit)",
    "CREATE REL TABLE IF NOT EXISTS INCLUDES (FROM Commit TO Change)",
    "CREATE REL TABLE IF NOT EXISTS AFFECTS_FUNC (FROM Change TO Function)",
    "CREATE REL TABLE IF NOT EXISTS AFFECTS_CLASS (FROM Change TO Class)",
    "CREATE REL TABLE IF NOT EXISTS CO_CHANGES_WITH (FROM File TO File, count INT64, correlation DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS PARENT (FROM Commit TO Commit)",
]

# ---------------------------------------------------------------------------
# Layer 3: Planning
# ---------------------------------------------------------------------------

PLANNING_NODE_TABLES = [
    """CREATE NODE TABLE IF NOT EXISTS Plan (
        id STRING,
        title STRING,
        description STRING,
        status STRING,
        created_at STRING,
        updated_at STRING,
        author STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Intent (
        id STRING,
        description STRING,
        rationale STRING,
        status STRING,
        PRIMARY KEY (id)
    )""",
]

PLANNING_REL_TABLES = [
    "CREATE REL TABLE IF NOT EXISTS TARGETS_FILE (FROM Plan TO File)",
    "CREATE REL TABLE IF NOT EXISTS TARGETS_MODULE (FROM Plan TO Module)",
    "CREATE REL TABLE IF NOT EXISTS TARGETS_CLASS (FROM Plan TO Class)",
    "CREATE REL TABLE IF NOT EXISTS TARGETS_FUNC (FROM Plan TO Function)",
    "CREATE REL TABLE IF NOT EXISTS IMPLEMENTS (FROM Intent TO Plan)",
    "CREATE REL TABLE IF NOT EXISTS DEPENDS_ON_PLAN (FROM Plan TO Plan)",
    "CREATE REL TABLE IF NOT EXISTS SUPERSEDES (FROM Plan TO Plan)",
]

# ---------------------------------------------------------------------------
# All tables, in creation order
# ---------------------------------------------------------------------------

ALL_STATEMENTS = (
    STRUCTURE_NODE_TABLES
    + STRUCTURE_REL_TABLES
    + HISTORY_NODE_TABLES
    + HISTORY_REL_TABLES
    + PLANNING_NODE_TABLES
    + PLANNING_REL_TABLES
)
