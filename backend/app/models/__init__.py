"""SQLAlchemy model package — imports every model module.

Importing the package (which happens whenever any ``app.models.*`` submodule
is imported) registers EVERY mapped class on the shared declarative Base.
SQLAlchemy resolves string-named ``relationship(...)`` targets at mapper
configuration time from this registry, so partial imports (e.g. a unit test
importing only ``app.models.processing_job``) must still be able to see
``DocumentComparison``/``DocumentVersion``/etc.  Keep this list complete
whenever a new model class is added.
"""
from app.models.base import Base, TimestampMixin  # noqa: F401
from app.models.organization import Organization  # noqa: F401
from app.models.user import (  # noqa: F401
    AuditLog,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.models.document import (  # noqa: F401
    Collection,
    CollectionDocument,
    Document,
    DocumentChunk,
    DocumentPage,
    DocumentSection,
    DocumentTag,
    DocumentVersion,
    PgVector,
)
from app.models.processing_job import ProcessingJob  # noqa: F401
from app.models.comparison import ComparisonChange, DocumentComparison  # noqa: F401
from app.models.conflict import Conflict, ConflictStatement  # noqa: F401
from app.models.summary import DocumentSummary  # noqa: F401
from app.models.extraction import (  # noqa: F401
    DocumentExtraction,
    DocumentExtractionItem,
)
from app.models.conversation import (  # noqa: F401
    Conversation,
    ConversationDocument,
    MessageFeedback,
)
from app.models.message import Citation, Message  # noqa: F401

__all__ = [
    "Base",
    "TimestampMixin",
    "Organization",
    "AuditLog",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "Collection",
    "CollectionDocument",
    "Document",
    "DocumentChunk",
    "DocumentPage",
    "DocumentSection",
    "DocumentTag",
    "DocumentVersion",
    "PgVector",
    "ProcessingJob",
    "ComparisonChange",
    "DocumentComparison",
    "Conflict",
    "ConflictStatement",
    "DocumentSummary",
    "DocumentExtraction",
    "DocumentExtractionItem",
    "Conversation",
    "ConversationDocument",
    "MessageFeedback",
    "Citation",
    "Message",
]
