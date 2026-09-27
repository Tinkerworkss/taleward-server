"""Anmeldung, Serverinfo, eigenes Konto, Organisationen."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, schemas
from app.access import current_user
from app.einstellungen import angaben
from app.extern import anbieter
from app.konto import registrierung
from app.db import get_db, server_id, utcnow
from app.models import AuthMethod, Organization, OrgMember, User
from app.security import create_token, verify_password
from app.services import user_out

API_VERSION = "0.4.0"

router = APIRouter(tags=["Auth"])


@router.get("/info", response_model=schemas.ServerInfoOut)
def info(db: Session = Depends(get_db)):
    from app import anmeldedienste, mail
    from app.einrichtung import betriebsart
    from app.einstellungen import llm_konfig
    from app.services import cloud_anbieter

    a = angaben(db)  # .env, überschreibbar in der Verwaltung
    extern = anbieter(db)
    k = llm_konfig(db)
    dienste = anmeldedienste.eingerichtet(db)
    return schemas.ServerInfoOut(
        name=a.server_name, operator=a.server_operator, contact=a.server_contact, api_version=API_VERSION,
        registration=registrierung(db), auth_methods=["password"] + (["oidc"] if dienste else []),
        privacy_policy_url=a.privacy_policy_url,
        min_age=a.min_age, external_transcription=extern, min_app_version=a.app_min_version,
        latest_app_version=a.app_latest_version, app_download_url=a.app_download_url, release_notes=a.app_release_notes,
        external_transcription_mode=(("primary" if betriebsart(db) == "cloud" else "fallback") if extern else None),
        cloud_summary=cloud_anbieter(k) if k.art == "api" and k.api_key else None,
        auth_providers=[schemas.AuthProviderOut(id=d, name=anmeldedienste.DIENSTE[d]["name"]) for d in dienste],
        password_reset=mail.kann_senden(db),
    )


@router.post("/auth/login", response_model=schemas.LoginResponse)
def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    methode = None
    if user is not None:
        methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id, AuthMethod.kind == "password"))
    if not verify_password(body.password, methode.secret if methode else None):
        raise errors.BAD_CREDENTIALS
    if user.setup_account:  # admin/admin: nur für die Ersteinrichtung in der Verwaltung
        raise errors.ApiError(401, "setup_account_only")
    methode.last_used_at = utcnow()
    db.commit()
    token, expires = create_token(user.id, user.token_version, server_id())
    return schemas.LoginResponse(access_token=token, expires_at=expires, user=user_out(user))


@router.post("/auth/register", status_code=201, response_model=schemas.LoginResponse)
def register(body: schemas.RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Konto mit Einladungscode anlegen und gleich anmelden. Beitreten macht die App danach mit /campaigns/join."""
    from app.konto import registrieren

    adresse = request.client.host if request.client else "?"
    if body.registration_token is not None:  # 0.4.0: nach Anmeldung mit einem Dienst
        from app.routers.anmeldung import registrieren_mit_dienst

        u = registrieren_mit_dienst(db, adresse, body)
    else:
        if body.username is None or body.password is None:
            raise errors.bad_request("validation_error", "validation_error.empty", field="username/password")
        u = registrieren(db, adresse, body.invite_code, body.username, body.display_name, body.password,
                         body.accept_privacy, body.age_confirmed)
    db.commit()
    token, expires = create_token(u.id, u.token_version, server_id())
    return schemas.LoginResponse(access_token=token, expires_at=expires, user=user_out(u))


@router.get("/me", response_model=schemas.UserOut)
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.get("/me/export")
def export(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    import json

    from app.konto import exportieren

    daten = exportieren(db, user, str(request.base_url).rstrip("/"))
    name = f"taleward-export-{user.username}-{utcnow():%Y%m%d}.json"
    return Response(json.dumps(daten, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.delete("/me", status_code=204)
def delete_me(body: schemas.DeleteMeRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.konto import loeschen

    loeschen(db, user, body.password, body.confirm_username)
    db.commit()
    return Response(status_code=204)


@router.get("/organizations", tags=["Kampagnen"], response_model=list[schemas.OrganizationOut])
def organizations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Organization, OrgMember.role).join(OrgMember, OrgMember.organization_id == Organization.id)
        .where(OrgMember.user_id == user.id).order_by(Organization.name)
    ).all()
    return [schemas.OrganizationOut(id=o.id, name=o.name, my_role=role) for o, role in rows]

