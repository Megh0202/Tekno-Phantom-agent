from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas import ProjectCreateRequest, ProjectListResponse, ProjectState, ProjectUpdateRequest

router = APIRouter(prefix="/api/projects", tags=["projects"])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_project_state(project: Project) -> ProjectState:
    return ProjectState(
        id=project.id,
        user_id=project.user_id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.post("", response_model=ProjectState, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectState:
    now = utc_now()
    project = Project(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _to_project_state(project)


@router.get("", response_model=ProjectListResponse)
def list_projects(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    statement = (
        select(Project)
        .where(Project.user_id == user.id)
        .order_by(Project.updated_at.desc(), Project.created_at.desc())
    )
    projects = db.execute(statement).scalars().all()
    return ProjectListResponse(items=[_to_project_state(project) for project in projects])


@router.put("/{project_id}", response_model=ProjectState)
def update_project(
    project_id: int,
    payload: ProjectUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectState:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    project.updated_at = utc_now()
    db.add(project)
    db.commit()
    db.refresh(project)
    return _to_project_state(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    db.delete(project)
    db.commit()
