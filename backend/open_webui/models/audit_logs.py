import logging
import time
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, Index, String, Text, Integer
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.internal.db import Base, get_async_db_context

log = logging.getLogger(__name__)

####################
# DB MODEL
####################


class AuditLog(Base):
    __tablename__ = 'audit_log'

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=True, index=True)
    user_name = Column(String, nullable=True)
    user_email = Column(String, nullable=True)
    user_role = Column(String, nullable=True)
    audit_level = Column(String, nullable=False)
    verb = Column(String, nullable=False)
    request_uri = Column(Text, nullable=False)
    response_status_code = Column(Integer, nullable=True)
    source_ip = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    request_object = Column(Text, nullable=True)
    response_object = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index('ix_audit_log_created_at', 'created_at'),
        Index('ix_audit_log_verb', 'verb'),
    )


class AuditLogModel(BaseModel):
    id: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    user_role: Optional[str] = None
    audit_level: str
    verb: str
    request_uri: str
    response_status_code: Optional[int] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    request_object: Optional[str] = None
    response_object: Optional[str] = None
    created_at: int


####################
# FUNCTIONS
####################


class AuditLogs:
    @staticmethod
    async def insert(
        entry_id: str,
        user: dict,
        audit_level: str,
        verb: str,
        request_uri: str,
        response_status_code: Optional[int] = None,
        source_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_object: Optional[str] = None,
        response_object: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[AuditLogModel]:
        async with get_async_db_context(db) as session:
            try:
                audit_log = AuditLog(
                    id=entry_id,
                    user_id=user.get('id'),
                    user_name=user.get('name'),
                    user_email=user.get('email'),
                    user_role=user.get('role'),
                    audit_level=audit_level,
                    verb=verb,
                    request_uri=request_uri,
                    response_status_code=response_status_code,
                    source_ip=source_ip,
                    user_agent=user_agent,
                    request_object=request_object,
                    response_object=response_object,
                    created_at=int(time.time()),
                )
                session.add(audit_log)
                await session.commit()
                await session.refresh(audit_log)
                return AuditLogModel.model_validate(audit_log, from_attributes=True)
            except Exception as e:
                log.error(f'Failed to insert audit log: {e}')
                await session.rollback()
                return None
