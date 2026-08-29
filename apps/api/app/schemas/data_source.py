import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DataSourceOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    file_type: str
    uploaded_at: datetime
    row_count: int
    status: str

    class Config:
        from_attributes = True


class UploadPreviewResponse(BaseModel):
    id: uuid.UUID
    name: str
    file_type: str
    row_count: int
    status: str
    columns: list[str]
    preview: list[dict]


class MapRequest(BaseModel):
    measure_column: str
    date_column: Optional[str] = None
    category_column: Optional[str] = None
    label_column: Optional[str] = None


class MapResponse(BaseModel):
    data_source_id: uuid.UUID
    inserted_rows: int
    status: str
