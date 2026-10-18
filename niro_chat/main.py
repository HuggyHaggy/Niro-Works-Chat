import os, re, sqlite3, secrets, hashlib, hmac, base64, json, time, asyncio, shutil, tempfile, zipfile, io
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, UploadFile, File, Header
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
# Keep user data outside the extracted project folder so replacing/updating the
# source files does not erase communities, accounts, messages, or uploaded avatars.
DATA_DIR = Path(os.environ.get('NIRO_DATA_DIR', str(Path.home() / '.niro_chat'))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'niro_chat.db'
UPLOAD_DIR = DATA_DIR / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# One-time migration from older versions that stored data inside the project.
# Also check sibling project folders, in case the updated ZIP was extracted to a
# new folder. The newest candidate is selected only when the persistent DB is absent.
LEGACY_DB_PATH = BASE / 'niro_chat.db'
MIGRATED_FROM = None
if not DB_PATH.exists():
    candidates = [LEGACY_DB_PATH, Path.cwd() / 'niro_chat.db']
    try: candidates.extend(BASE.parent.glob('*/niro_chat.db'))
    except OSError: pass
    candidates = [x.resolve() for x in candidates if x.exists() and x.is_file() and x.resolve() != DB_PATH]
    if candidates:
        source_path = max(set(candidates), key=lambda x: x.stat().st_mtime)
        # SQLite's backup API also captures committed data safely when the source used WAL.
        source_conn = sqlite3.connect(str(source_path), timeout=30)
        target_conn = sqlite3.connect(str(DB_PATH), timeout=30)
        try: source_conn.backup(target_conn)
        finally: target_conn.close(); source_conn.close()
        MIGRATED_FROM = source_path.parent
LEGACY_UPLOAD_DIRS = [BASE / 'static' / 'uploads']
if MIGRATED_FROM is not None: LEGACY_UPLOAD_DIRS.append(MIGRATED_FROM / 'static' / 'uploads')
for legacy_upload_dir in LEGACY_UPLOAD_DIRS:
    if legacy_upload_dir.exists():
        for old_file in legacy_upload_dir.iterdir():
            target = UPLOAD_DIR / old_file.name
            if old_file.is_file() and not target.exists():
                shutil.copy2(old_file, target)
SECRET = os.environ.get('NIRO_SECRET', 'change-this-secret-before-deploying')
NIRO_ENV = os.environ.get('NIRO_ENV', 'development').strip().lower()
ALLOWED_ORIGINS = [x.strip().rstrip('/') for x in os.environ.get('NIRO_ALLOWED_ORIGINS', '').split(',') if x.strip()]
if NIRO_ENV == 'production' and (SECRET == 'change-this-secret-before-deploying' or len(SECRET) < 32):
    raise RuntimeError('Em produção, defina NIRO_SECRET com pelo menos 32 caracteres aleatórios.')
if NIRO_ENV == 'production' and not ALLOWED_ORIGINS:
    raise RuntimeError('Em produção, defina NIRO_ALLOWED_ORIGINS com o domínio HTTPS do frontend.')
app = FastAPI(title='Niro Chat API', version='0.2.0')
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS or ['*'], allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory=BASE/'static'), name='static')
app.mount('/uploads', StaticFiles(directory=UPLOAD_DIR), name='uploads')


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA synchronous = FULL')
    return conn

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'online', dm_policy TEXT NOT NULL DEFAULT 'friends', friend_request_policy TEXT NOT NULL DEFAULT 'everyone', profile_visibility TEXT NOT NULL DEFAULT 'everyone', show_activity INTEGER NOT NULL DEFAULT 1, settings TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS friendships(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, friend_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, UNIQUE(user_id,friend_id));
    CREATE TABLE IF NOT EXISTS communities(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', owner_id INTEGER NOT NULL REFERENCES users(id), invite_code TEXT UNIQUE NOT NULL, settings TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS community_members(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'Member', joined_at INTEGER NOT NULL, timeout_until INTEGER NOT NULL DEFAULT 0, UNIQUE(community_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text', settings TEXT NOT NULL DEFAULT '{}', folder_id INTEGER, position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS community_roles(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#b6f36b', permissions TEXT NOT NULL DEFAULT '{}', settings TEXT NOT NULL DEFAULT '{}', position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, UNIQUE(community_id,name));
    CREATE TABLE IF NOT EXISTS member_custom_roles(community_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_id INTEGER NOT NULL REFERENCES community_roles(id) ON DELETE CASCADE, assigned_at INTEGER NOT NULL, PRIMARY KEY(community_id,user_id,role_id), FOREIGN KEY(community_id,user_id) REFERENCES community_members(community_id,user_id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), content TEXT NOT NULL, created_at INTEGER NOT NULL, edited_at INTEGER, message_type TEXT NOT NULL DEFAULT 'chat');
    CREATE TABLE IF NOT EXISTS community_folders(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS community_events(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', starts_at TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS gallery_items(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', media_url TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS poll_votes(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, option_index INTEGER NOT NULL, voted_at INTEGER NOT NULL, PRIMARY KEY(message_id,user_id));
    CREATE TABLE IF NOT EXISTS community_bans(community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, banned_by INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, PRIMARY KEY(community_id,user_id));
    CREATE TABLE IF NOT EXISTS dm_messages(id INTEGER PRIMARY KEY, sender_id INTEGER NOT NULL REFERENCES users(id), receiver_id INTEGER NOT NULL REFERENCES users(id), content TEXT NOT NULL, created_at INTEGER NOT NULL);
    ''')
    # Migrations for databases created by earlier versions.
    cols={r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    for name, ddl in [('dm_policy', "TEXT NOT NULL DEFAULT 'friends'"), ('friend_request_policy', "TEXT NOT NULL DEFAULT 'everyone'"), ('profile_visibility', "TEXT NOT NULL DEFAULT 'everyone'"), ('show_activity', 'INTEGER NOT NULL DEFAULT 1'), ('settings', "TEXT NOT NULL DEFAULT '{}'")]:
        if name not in cols: c.execute(f'ALTER TABLE users ADD COLUMN {name} {ddl}')
    for table in ('communities','channels','community_roles'):
        existing={r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
        if 'settings' not in existing: c.execute(f"ALTER TABLE {table} ADD COLUMN settings TEXT NOT NULL DEFAULT '{{}}'")
    member_cols={r['name'] for r in c.execute('PRAGMA table_info(community_members)').fetchall()}
    if 'timeout_until' not in member_cols: c.execute('ALTER TABLE community_members ADD COLUMN timeout_until INTEGER NOT NULL DEFAULT 0')
    channel_cols={r['name'] for r in c.execute('PRAGMA table_info(channels)').fetchall()}
    if 'folder_id' not in channel_cols: c.execute('ALTER TABLE channels ADD COLUMN folder_id INTEGER')
    if 'position' not in channel_cols: c.execute('ALTER TABLE channels ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
    message_cols={r['name'] for r in c.execute('PRAGMA table_info(messages)').fetchall()}
    if 'message_type' not in message_cols: c.execute("ALTER TABLE messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'chat'")
    # Voice chat was not requested. Keep any old channel content but convert legacy voice channels to ordinary text channels.
    c.execute("UPDATE channels SET kind='text' WHERE kind!='text'")
    # Move legacy avatar URLs to the persistent upload directory.
    c.execute("UPDATE users SET avatar = REPLACE(avatar, '/static/uploads/', '/uploads/') WHERE avatar LIKE '/static/uploads/%'")
    c.commit(); c.close()
init_db()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 210000)
    return base64.urlsafe_b64encode(salt).decode() + '$' + base64.urlsafe_b64encode(dk).decode()

def verify_password(password: str, saved: str) -> bool:
    try:
        salt_s, hash_s = saved.split('$', 1)
        salt = base64.urlsafe_b64decode(salt_s.encode())
        expected = base64.urlsafe_b64decode(hash_s.encode())
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 210000)
        return hmac.compare_digest(actual, expected)
    except Exception: return False

def make_token(user_id: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({'uid':user_id,'exp':int(time.time())+60*60*24*14}).encode()).decode().rstrip('=')
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()).decode().rstrip('=')
    return payload + '.' + sig

def token_user(token: str):
    try:
        payload, sig = token.split('.',1)
        expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()).decode().rstrip('=')
        if not hmac.compare_digest(sig, expected): raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + '='*((4-len(payload)%4)%4)))
        if data['exp'] < int(time.time()): raise ValueError()
        c=db(); user=c.execute('SELECT * FROM users WHERE id=?',(data['uid'],)).fetchone(); c.close()
        if not user: raise ValueError()
        return user
    except Exception: raise HTTPException(401, 'Sessão inválida ou expirada')

def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'Faça login para continuar')
    return token_user(authorization.split(' ',1)[1])

def user_public(u):
    keys=set(u.keys())
    return {'id':u['id'],'username':u['username'],'display_name':u['display_name'],'bio':u['bio'],'avatar':u['avatar'],'status':u['status'],'dm_policy':u['dm_policy'] if 'dm_policy' in keys else 'friends','friend_request_policy':u['friend_request_policy'] if 'friend_request_policy' in keys else 'everyone','profile_visibility':u['profile_visibility'] if 'profile_visibility' in keys else 'everyone','show_activity':bool(u['show_activity']) if 'show_activity' in keys else True,'settings':json.loads(u['settings'] or '{}') if 'settings' in keys else {},'created_at':u['created_at']}

def public_user_for_viewer(c, row, viewer_id):
    public=user_public(row)
    if row['id']==viewer_id: return public
    friendship=c.execute('SELECT status FROM friendships WHERE ((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)) LIMIT 1',(viewer_id,row['id'],row['id'],viewer_id)).fetchone()
    is_friend=bool(friendship and friendship['status']=='accepted')
    keys=set(row.keys())
    if ('profile_visibility' in keys and row['profile_visibility'] in ('friends','nobody') and not is_friend):
        public.update({'bio':'','avatar':'','status':'invisible'})
    elif ('profile_visibility' in keys and row['profile_visibility']=='nobody' and is_friend):
        public.update({'bio':'','avatar':'','status':'invisible'})
    elif 'show_activity' in keys and not row['show_activity']:
        public['status']='invisible'
    return public

def member_role(c, community_id, user_id):
    return c.execute('SELECT role FROM community_members WHERE community_id=? AND user_id=?',(community_id,user_id)).fetchone()

def require_member(c, community_id, user_id):
    m=member_role(c,community_id,user_id)
    if not m: raise HTTPException(403,'Você não faz parte desta comunidade')
    return m['role']

PERMISSION_KEYS = ['manage_community','manage_channels','manage_roles','manage_members','send_messages','read_messages','create_invites','manage_messages','pin_messages','delete_messages','timeout_members','kick_members','ban_members','bypass_slowmode']

def role_key_builtin(role):
    return 'builtin:' + role

def role_order(c, community_id):
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone()
    settings=json.loads(row['settings'] or '{}') if row else {}
    custom=c.execute('SELECT id,name,position,created_at FROM community_roles WHERE community_id=? ORDER BY position DESC,created_at,id',(community_id,)).fetchall()
    defaults=['builtin:Owner','builtin:Admin']+[f'custom:{r["id"]}' for r in custom]+['builtin:Moderator','builtin:Member']
    saved=settings.get('role_order')
    if isinstance(saved,list):
        allowed=set(defaults); ordered=[x for x in saved if x in allowed]
        ordered += [x for x in defaults if x not in ordered]
        # Owner is immutable at the top and Member remains present.
        ordered=[x for x in ordered if x!='builtin:Owner']
        ordered.insert(0,'builtin:Owner')
        if 'builtin:Member' in ordered and ordered[-1] != 'builtin:Member':
            ordered.remove('builtin:Member'); ordered.append('builtin:Member')
        return ordered
    return defaults

def role_permissions_for_key(c, community_id, key):
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone()
    community_settings=json.loads(row['settings'] or '{}') if row else {}
    matrix=community_settings.get('role_permission_matrix',{})
    explicit=matrix.get(key,{}) if isinstance(matrix,dict) else {}
    if key=='builtin:Owner': return {k:True for k in PERMISSION_KEYS}
    if isinstance(explicit,dict) and explicit:
        # Missing toggles are denied once a role has an explicitly configured matrix.
        return {k:bool(explicit.get(k,False)) for k in PERMISSION_KEYS}
    if key.startswith('builtin:'):
        role=key.split(':',1)[1]
        if role=='Admin': return {k:True for k in PERMISSION_KEYS}
        if role in ('Moderator','Member'):
            return {k:(k in ('read_messages','send_messages')) for k in PERMISSION_KEYS}
    if key.startswith('custom:'):
        try: rid=int(key.split(':',1)[1])
        except ValueError: return {}
        row=c.execute('SELECT permissions,settings FROM community_roles WHERE id=? AND community_id=?',(rid,community_id)).fetchone()
        if not row:return {}
        perms=json.loads(row['permissions'] or '{}'); settings=json.loads(row['settings'] or '{}')
        mapping={'manage_messages':'can_manage_messages','pin_messages':'can_pin_messages','delete_messages':'can_delete_messages','timeout_members':'can_timeout_members','kick_members':'can_kick_members','ban_members':'can_ban_members','bypass_slowmode':'can_bypass_slowmode','create_invites':'can_create_invites','manage_channels':'can_manage_channel_settings','manage_community':'can_manage_profile'}
        out={k:bool(perms.get(k,False)) for k in PERMISSION_KEYS}
        for permission, setting in mapping.items():
            if setting in settings: out[permission]=bool(settings[setting])
        return out
    return {}

def user_role_keys(c, community_id, user_id):
    base=member_role(c,community_id,user_id)
    if not base:return []
    keys=[role_key_builtin(base['role'])]
    keys += [f'custom:{r[0]}' for r in c.execute('SELECT role_id FROM member_custom_roles WHERE community_id=? AND user_id=?',(community_id,user_id)).fetchall()]
    order=role_order(c,community_id); ranks={k:i for i,k in enumerate(order)}
    return sorted(set(keys),key=lambda k:ranks.get(k,9999))

def effective_role_key(c, community_id, user_id):
    keys=user_role_keys(c,community_id,user_id)
    return keys[0] if keys else None

def role_rank(c, community_id, user_id):
    key=effective_role_key(c,community_id,user_id)
    order=role_order(c,community_id)
    return order.index(key) if key in order else 9999

def role_ceiling_allows(c, community_id, key, permission):
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone()
    settings=json.loads(row['settings'] or '{}') if row else {}
    matrix=settings.get('role_permission_matrix',{})
    if isinstance(matrix,dict) and key in matrix and isinstance(matrix[key],dict) and permission in matrix[key]:
        return bool(matrix[key][permission])
    return True

def has_community_permission(c, community_id, user_id, permission):
    if not member_role(c,community_id,user_id): return False
    key=effective_role_key(c,community_id,user_id)
    if key=='builtin:Owner': return True
    order=role_order(c,community_id)
    if key not in order: return False
    idx=order.index(key)
    own=role_permissions_for_key(c,community_id,key)
    if not own.get(permission,False): return False
    # Permission ceiling: a lower role cannot grant itself something denied by any higher role.
    for higher in order[1:idx]:
        if not role_ceiling_allows(c,community_id,higher,permission): return False
    return True

def can_manage_target(c, community_id, actor_id, target_id):
    if actor_id==target_id:return False
    return role_rank(c,community_id,actor_id) < role_rank(c,community_id,target_id)

def can_manage_role(c, community_id, actor_id, role_id):
    actor=member_role(c,community_id,actor_id)
    if actor and actor['role']=='Owner': return True
    target=c.execute('SELECT id FROM community_roles WHERE id=? AND community_id=?',(role_id,community_id)).fetchone()
    if not target:return False
    order=role_order(c,community_id); key=f'custom:{role_id}'
    return role_rank(c,community_id,actor_id) < (order.index(key) if key in order else 9999)

def can_view_channel(c, channel, user_id):
    settings=json.loads(channel['settings'] or '{}')
    if not has_community_permission(c,channel['community_id'],user_id,'read_messages'): return False
    if settings.get('visible_to_all',True): return True
    # Private channel: only Owner/Admin/Moderator or a role allowed to manage channels.
    keys=user_role_keys(c,channel['community_id'],user_id)
    if any(k in ('builtin:Owner','builtin:Admin','builtin:Moderator') for k in keys): return True
    return has_community_permission(c,channel['community_id'],user_id,'manage_channels')

def slowmode_applies(c, community_id, user_id, settings):
    if has_community_permission(c,community_id,user_id,'bypass_slowmode'): return False
    threshold=settings.get('slowmode_role_key','all')
    if not threshold or threshold=='all': return True
    order=role_order(c,community_id)
    keys=user_role_keys(c,community_id,user_id)
    if not keys:return True
    ranks={k:i for i,k in enumerate(order)}
    actor_rank=min([ranks.get(k,9999) for k in keys],default=9999)
    threshold_rank=ranks.get(str(threshold),9999)
    return actor_rank >= threshold_rank

def require_permission(c, community_id, user_id, permission, message='Você não tem permissão para esta ação'):
    require_member(c,community_id,user_id)
    if not has_community_permission(c,community_id,user_id,permission): raise HTTPException(403,message)

class RegisterIn(BaseModel):
    username: str = Field(min_length=3,max_length=24, pattern=r'^[a-zA-Z0-9_.-]+$')
    email: str = Field(min_length=5,max_length=254)
    password: str = Field(min_length=8,max_length=128)
    display_name: str = Field(min_length=1,max_length=40)
class LoginIn(BaseModel):
    login: str
    password: str
class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1,max_length=40)
    bio: str = Field(default='',max_length=190)
    status: str = 'online'
    dm_policy: str = 'friends'
    friend_request_policy: str = 'everyone'
    profile_visibility: str = 'everyone'
    show_activity: bool = True
class CommunityIn(BaseModel):
    name: str = Field(min_length=2,max_length=60)
    description: str = Field(default='',max_length=300)
    icon: str = Field(default='',max_length=8)
class ChannelIn(BaseModel):
    name: str = Field(min_length=1,max_length=50,pattern=r'^[a-zA-Z0-9_-]+$')
    kind: str = 'text'
class MessageIn(BaseModel):
    content: str = Field(min_length=1,max_length=4000)
    message_type: str = 'chat'
class FriendIn(BaseModel):
    username: str
class RoleIn(BaseModel):
    role: str = Field(min_length=2,max_length=32)
class CommunityUpdate(BaseModel):
    name: str = Field(min_length=2,max_length=60)
    description: str = Field(default='',max_length=300)
    icon: str = Field(default='',max_length=8)

class CustomRoleIn(BaseModel):
    name: str = Field(min_length=2,max_length=32)
    color: str = Field(default='#b6f36b', pattern=r'^#[0-9a-fA-F]{6}$')
    permissions: dict = Field(default_factory=dict)

class AssignRoleIn(BaseModel):
    role_id: int

class SettingsIn(BaseModel):
    settings: dict = Field(default_factory=dict)

PROFILE_SETTING_KEYS = {
 'dm_policy','friend_request_policy','profile_visibility','show_activity','theme','accent_color','font_size','compact_mode','reduced_motion','language','timezone','date_format','message_density','show_email','allow_mentions','allow_friend_requests','allow_group_invites','allow_server_invites','dm_receipts','read_receipts','show_online_status','show_activity_details','profile_visibility_detail','friend_list_visibility','mutual_communities_visibility','allow_profile_comments','filter_unknown_dms','filter_links','blocked_words','notification_dms','notification_friend_requests','notification_community_invites','notification_mentions','notification_sounds','desktop_notifications'
}
COMMUNITY_SETTING_KEYS = {
 'category','language','region','visibility','discoverable','invite_only','allow_member_invites','invite_expiry_hours','member_limit','verification_level','default_notifications','system_messages','welcome_message','rules_text','community_color','banner_url','icon_style','show_member_count','show_online_count','allow_custom_emojis','allow_external_links','content_filter','slowmode_default','default_channel_id','new_member_role','require_rules_acceptance','allow_member_profiles','audit_log_enabled','community_age_gate','description_public'
}
CHANNEL_SETTING_KEYS = {
 'topic','description','channel_color','channel_icon','sort_order','read_only','slowmode_seconds','slowmode_role_key','pin_messages','allow_links','allow_attachments','allow_embeds','allow_reactions','allow_mentions_everyone','allow_threads','default_slowmode','notification_default','archive_after_days','visible_to_all','age_restricted','channel_welcome'
}
ROLE_SETTING_KEYS = {
 'display_label','description','hoist','mentionable','icon','priority','expires_days','can_create_invites','can_manage_messages','can_pin_messages','can_delete_messages','can_timeout_members','can_kick_members','can_ban_members','can_manage_profile','can_manage_channel_settings','can_view_audit_log','can_manage_invites','can_manage_emojis','can_bypass_slowmode'
}

@app.get('/')
def home(): return FileResponse(BASE/'static'/'index.html')
@app.get('/api/health')
def health():
    c=db()
    try:
        counts={table:c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('users','communities','community_members','channels','messages','friendships','dm_messages','community_roles')}
    finally: c.close()
    return {'ok':True,'service':'Niro Chat API','storage':'persistent SQLite','counts':counts}

@app.get('/api/backup')
def download_backup(u=Depends(current_user)):
    # A user-triggered full backup. SQLite's backup API creates a consistent snapshot
    # even while the app is running; avatar uploads are included as well.
    with tempfile.TemporaryDirectory(prefix='niro-backup-') as tmp:
        snapshot=Path(tmp)/'niro_chat.db'
        source=db()
        try:
            target=sqlite3.connect(snapshot)
            source.backup(target)
            target.close()
        finally: source.close()
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot,'niro_chat.db')
            for file in UPLOAD_DIR.iterdir():
                if file.is_file(): zf.write(file, f'uploads/{file.name}')
            zf.writestr('BACKUP-INFO.txt', 'Niro Chat backup\nContains the SQLite database and uploaded profile images.\nKeep this archive private. Restore only after stopping the server.\n')
        archive.seek(0)
        return StreamingResponse(archive,media_type='application/zip',headers={'Content-Disposition':'attachment; filename=niro-chat-backup.zip'})

@app.post('/api/register')
def register(data:RegisterIn):
    email=data.email.strip().lower(); username=data.username.strip()
    if '@' not in email or '.' not in email.rsplit('@',1)[-1]: raise HTTPException(400,'E-mail inválido')
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)',(username,email,hash_password(data.password),data.display_name.strip(),int(time.time())))
        uid=cur.lastrowid; c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        return {'token':make_token(uid),'user':user_public(u)}
    except sqlite3.IntegrityError:
        raise HTTPException(409,'Nome de usuário ou e-mail já cadastrado')
    finally: c.close()

@app.post('/api/login')
def login(data:LoginIn):
    c=db(); u=c.execute('SELECT * FROM users WHERE lower(username)=lower(?) OR lower(email)=lower(?)',(data.login.strip(),data.login.strip())).fetchone(); c.close()
    if not u or not verify_password(data.password,u['password_hash']): raise HTTPException(401,'Usuário/e-mail ou senha incorretos')
    return {'token':make_token(u['id']),'user':user_public(u)}

@app.get('/api/me')
def me(u=Depends(current_user)): return user_public(u)

@app.patch('/api/me')
def update_me(data:ProfileIn,u=Depends(current_user)):
    if data.status not in ('online','idle','dnd','invisible'): raise HTTPException(400,'Status inválido')
    if data.dm_policy not in ('everyone','friends','nobody'): raise HTTPException(400,'Configuração de mensagens privadas inválida')
    if data.friend_request_policy not in ('everyone','nobody'): raise HTTPException(400,'Configuração de pedidos inválida')
    if data.profile_visibility not in ('everyone','friends'): raise HTTPException(400,'Visibilidade de perfil inválida')
    c=db(); c.execute('UPDATE users SET display_name=?,bio=?,status=?,dm_policy=?,friend_request_policy=?,profile_visibility=?,show_activity=? WHERE id=?',(data.display_name.strip(),data.bio.strip(),data.status,data.dm_policy,data.friend_request_policy,data.profile_visibility,int(data.show_activity),u['id'])); c.commit(); fresh=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.close(); return user_public(fresh)

@app.get('/api/me/settings')
def get_profile_settings(u=Depends(current_user)):
    return {'settings': json.loads(u['settings'] or '{}')}

@app.patch('/api/me/settings')
def patch_profile_settings(data:SettingsIn,u=Depends(current_user)):
    incoming={k:v for k,v in data.settings.items() if k in PROFILE_SETTING_KEYS}
    c=db(); row=c.execute('SELECT settings FROM users WHERE id=?',(u['id'],)).fetchone(); current=json.loads(row['settings'] or '{}')
    current.update(incoming)
    # Core privacy settings are stored in dedicated columns too, so the existing authorization checks enforce them.
    dm=current.get('dm_policy')
    if dm in ('everyone','friends','nobody'): c.execute('UPDATE users SET dm_policy=? WHERE id=?',(dm,u['id']))
    fr=current.get('friend_request_policy')
    if current.get('allow_friend_requests') is False: fr='nobody'
    elif current.get('allow_friend_requests') is True and fr=='nobody': fr='everyone'
    if fr in ('everyone','friends','nobody'): c.execute('UPDATE users SET friend_request_policy=? WHERE id=?',(fr,u['id']))
    vis=current.get('profile_visibility')
    detail_vis=current.get('profile_visibility_detail')
    if detail_vis in ('everyone','friends','nobody'): vis=detail_vis
    if vis in ('everyone','friends','nobody'): c.execute('UPDATE users SET profile_visibility=? WHERE id=?',(vis,u['id']))
    if 'show_activity' in current: c.execute('UPDATE users SET show_activity=? WHERE id=?',(1 if current['show_activity'] else 0,u['id']))
    if 'show_online_status' in current: c.execute('UPDATE users SET show_activity=? WHERE id=?',(1 if current['show_online_status'] else 0,u['id']))
    c.execute('UPDATE users SET settings=? WHERE id=?',(json.dumps(current,ensure_ascii=False),u['id'])); c.commit(); result=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.close()
    return user_public(result)

@app.post('/api/me/avatar')
async def upload_avatar(file: UploadFile=File(...),u=Depends(current_user)):
    if file.content_type not in ('image/png','image/jpeg','image/webp','image/gif'): raise HTTPException(415,'Use PNG, JPG, WEBP ou GIF')
    data=await file.read()
    if len(data)>3*1024*1024: raise HTTPException(413,'A imagem deve ter no máximo 3 MB')
    ext={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[file.content_type]
    name=f'user_{u["id"]}_{secrets.token_hex(5)}.{ext}'; (UPLOAD_DIR/name).write_bytes(data)
    path='/uploads/'+name
    c=db(); c.execute('UPDATE users SET avatar=? WHERE id=?',(path,u['id'])); c.commit(); c.close(); return {'avatar':path}

@app.get('/api/users/search')
def search_users(q:str,u=Depends(current_user)):
    q=q.strip()
    if len(q)<2: return []
    c=db(); rows=c.execute('SELECT * FROM users WHERE id!=? AND (username LIKE ? OR display_name LIKE ?) ORDER BY username LIMIT 20',(u['id'],f'%{q}%',f'%{q}%')).fetchall()
    out=[]
    for r in rows:
        f=c.execute('SELECT * FROM friendships WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)',(u['id'],r['id'],r['id'],u['id'])).fetchone()
        public=public_user_for_viewer(c,r,u['id'])
        out.append({**public,'friendship_status':f['status'] if f else None,'friendship_direction':('incoming' if f and f['status']=='pending' and f['friend_id']==u['id'] else 'outgoing' if f and f['status']=='pending' and f['user_id']==u['id'] else 'friend' if f and f['status']=='accepted' else None)})
    c.close(); return out

@app.get('/api/friends')
def friends(u=Depends(current_user)):
    c=db(); rows=c.execute('''SELECT f.status,f.user_id,f.friend_id, x.* FROM friendships f JOIN users x ON x.id=CASE WHEN f.user_id=? THEN f.friend_id ELSE f.user_id END WHERE f.user_id=? OR f.friend_id=? ORDER BY x.username''',(u['id'],u['id'],u['id'])).fetchall()
    out=[]
    for r in rows:
        direction='incoming' if r['friend_id']==u['id'] and r['status']=='pending' else 'outgoing' if r['user_id']==u['id'] and r['status']=='pending' else 'friend'
        out.append({**user_public(r),'friendship_status':r['status'],'direction':direction})
    c.close(); return out

@app.post('/api/friends/request')
def friend_request(data:FriendIn,u=Depends(current_user)):
    c=db(); target=c.execute('SELECT * FROM users WHERE lower(username)=lower(?)',(data.username.strip().lstrip('@'),)).fetchone()
    if not target: c.close(); raise HTTPException(404,'Usuário não encontrado')
    if target['id']==u['id']: c.close(); raise HTTPException(400,'Você não pode adicionar a si mesmo')
    existing=c.execute('SELECT * FROM friendships WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)',(u['id'],target['id'],target['id'],u['id'])).fetchone()
    if existing:
        if existing['status']=='pending' and existing['user_id']==target['id']:
            c.execute('UPDATE friendships SET status="accepted" WHERE id=?',(existing['id'],)); c.commit(); c.close(); return {'ok':True,'message':'Pedido aceito'}
        c.close(); raise HTTPException(409,'Já existe um pedido ou amizade entre vocês')
    if target['friend_request_policy']=='nobody': c.close(); raise HTTPException(403,'Este usuário não está aceitando pedidos de amizade')
    c.execute('INSERT INTO friendships(user_id,friend_id,status,created_at) VALUES(?,?,?,?)',(u['id'],target['id'],'pending',int(time.time()))); c.commit(); c.close(); return {'ok':True,'message':'Pedido de amizade enviado'}

@app.post('/api/friends/{friend_id}/accept')
def accept_friend(friend_id:int,u=Depends(current_user)):
    c=db(); cur=c.execute('UPDATE friendships SET status="accepted" WHERE user_id=? AND friend_id=? AND status="pending"',(friend_id,u['id'])); c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,'Pedido não encontrado')
    return {'ok':True}

@app.delete('/api/friends/{friend_id}')
def remove_friend(friend_id:int,u=Depends(current_user)):
    c=db(); c.execute('DELETE FROM friendships WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)',(u['id'],friend_id,friend_id,u['id'])); c.commit(); c.close(); return {'ok':True}

@app.get('/api/dms/{other_id}')
def get_dms(other_id:int,u=Depends(current_user)):
    c=db(); target=c.execute('SELECT id,dm_policy FROM users WHERE id=?',(other_id,)).fetchone()
    if not target: c.close(); raise HTTPException(404,'Usuário não encontrado')
    f=c.execute('SELECT status FROM friendships WHERE ((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)) AND status="accepted"',(u['id'],other_id,other_id,u['id'])).fetchone()
    if target['dm_policy']=='nobody' or (target['dm_policy']=='friends' and not f): c.close(); raise HTTPException(403,'Este usuário não aceita mensagens privadas de você')
    rows=c.execute('SELECT m.*,u.username,u.display_name,u.avatar FROM dm_messages m JOIN users u ON u.id=m.sender_id WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?) ORDER BY id DESC LIMIT 100',(u['id'],other_id,other_id,u['id'])).fetchall(); c.close()
    return [dict(r) for r in reversed(rows)]

@app.post('/api/dms/{other_id}')
def send_dm(other_id:int,data:MessageIn,u=Depends(current_user)):
    content=data.content.strip()
    if not content: raise HTTPException(400,'Mensagem vazia')
    c=db(); target=c.execute('SELECT id,dm_policy FROM users WHERE id=?',(other_id,)).fetchone()
    if not target: c.close(); raise HTTPException(404,'Usuário não encontrado')
    f=c.execute('SELECT status FROM friendships WHERE ((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)) AND status="accepted"',(u['id'],other_id,other_id,u['id'])).fetchone()
    if target['dm_policy']=='nobody' or (target['dm_policy']=='friends' and not f): c.close(); raise HTTPException(403,'Este usuário não aceita mensagens privadas de você')
    cur=c.execute('INSERT INTO dm_messages(sender_id,receiver_id,content,created_at) VALUES(?,?,?,?)',(u['id'],other_id,content,int(time.time()))); c.commit(); r=c.execute('SELECT m.*,u.username,u.display_name,u.avatar FROM dm_messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?',(cur.lastrowid,)).fetchone(); c.close(); return dict(r)

@app.post('/api/communities')
def create_community(data:CommunityIn,u=Depends(current_user)):
    c=db(); invite=secrets.token_urlsafe(7).replace('-','').replace('_','')[:10]
    cur=c.execute('INSERT INTO communities(name,description,icon,owner_id,invite_code,created_at) VALUES(?,?,?,?,?,?)',(data.name.strip(),data.description.strip(),data.icon.strip(),u['id'],invite,int(time.time())))
    cid=cur.lastrowid; c.execute('INSERT INTO community_members(community_id,user_id,role,joined_at) VALUES(?,?,?,?)',(cid,u['id'],'Owner',int(time.time())))
    c.execute('INSERT INTO channels(community_id,name,kind,created_at) VALUES(?,?,?,?)',(cid,'geral','text',int(time.time())))
    c.commit(); row=c.execute('SELECT * FROM communities WHERE id=?',(cid,)).fetchone(); c.close(); return dict(row)

@app.get('/api/communities')
def list_communities(u=Depends(current_user)):
    c=db(); rows=c.execute('SELECT cm.role,co.* FROM community_members cm JOIN communities co ON co.id=cm.community_id WHERE cm.user_id=? ORDER BY co.name',(u['id'],)).fetchall(); result=[]
    for r in rows:
        x=dict(r); x['settings']=json.loads(x.get('settings') or '{}'); x['member_count']=c.execute('SELECT COUNT(*) n FROM community_members WHERE community_id=?',(r['id'],)).fetchone()['n']; result.append(x)
    c.close(); return result

@app.post('/api/communities/join/{invite_code}')
def join_community(invite_code:str,u=Depends(current_user)):
    c=db(); co=c.execute('SELECT * FROM communities WHERE invite_code=?',(invite_code,)).fetchone()
    if not co: c.close(); raise HTTPException(404,'Convite inválido')
    community_settings=json.loads(co['settings'] or '{}')
    if c.execute('SELECT 1 FROM community_bans WHERE community_id=? AND user_id=?',(co['id'],u['id'])).fetchone(): c.close(); raise HTTPException(403,'Você foi banido desta comunidade')
    existing_member=c.execute('SELECT 1 FROM community_members WHERE community_id=? AND user_id=?',(co['id'],u['id'])).fetchone()
    limit=int(community_settings.get('member_limit',0) or 0)
    if not existing_member and limit>0:
        count=c.execute('SELECT COUNT(*) n FROM community_members WHERE community_id=?',(co['id'],)).fetchone()['n']
        if count>=limit: c.close(); raise HTTPException(403,'Esta comunidade atingiu o limite de membros')
    default_role=community_settings.get('new_member_role','Member')
    if default_role not in ('Member','Moderator'): default_role='Member'
    try: c.execute('INSERT INTO community_members(community_id,user_id,role,joined_at) VALUES(?,?,?,?)',(co['id'],u['id'],default_role,int(time.time()))); c.commit()
    except sqlite3.IntegrityError: pass
    c.close(); return {'ok':True,'community_id':co['id']}

@app.get('/api/communities/{community_id}')
def community_detail(community_id:int,u=Depends(current_user)):
    c=db(); role=require_member(c,community_id,u['id']); co=c.execute('SELECT * FROM communities WHERE id=?',(community_id,)).fetchone()
    if not co: c.close(); raise HTTPException(404,'Comunidade não encontrada')
    channels=[]
    for r in c.execute("SELECT * FROM channels WHERE community_id=? AND kind='text' ORDER BY position,id",(community_id,)).fetchall():
        if can_view_channel(c,r,u['id']): channels.append({**dict(r),'settings':json.loads(r['settings'] or '{}')})
    folders=[dict(r) for r in c.execute('SELECT * FROM community_folders WHERE community_id=? ORDER BY position,id',(community_id,)).fetchall()]
    community_data=dict(co); community_data['settings']=json.loads(co['settings'] or '{}')
    roles=[]
    for rr in c.execute('SELECT * FROM community_roles WHERE community_id=? ORDER BY position DESC,id',(community_id,)).fetchall():
        rd=dict(rr); rd['permissions']=json.loads(rd['permissions']); rd['settings']=json.loads(rd.get('settings') or '{}'); roles.append(rd)
    members=[]
    for r in c.execute('SELECT cm.role,cm.joined_at,u.* FROM community_members cm JOIN users u ON u.id=cm.user_id WHERE cm.community_id=? ORDER BY CASE cm.role WHEN "Owner" THEN 0 WHEN "Admin" THEN 1 ELSE 2 END,u.username',(community_id,)).fetchall():
        members.append({**public_user_for_viewer(c,r,u['id']),'role':r['role'],'joined_at':r['joined_at']})
    for m in members:
        m['custom_roles']=[dict(x) for x in c.execute('SELECT r.id,r.name,r.color FROM member_custom_roles a JOIN community_roles r ON r.id=a.role_id WHERE a.community_id=? AND a.user_id=? ORDER BY r.position DESC',(community_id,m['id'])).fetchall()]
    channels.sort(key=lambda ch:(int(ch['settings'].get('sort_order',ch.get('position',0)) or 0),int(ch.get('position',0) or 0),int(ch['id'])))
    c.close()
    return {'community':community_data,'my_role':role,'channels':channels,'folders':folders,'members':members,'roles':roles}

@app.patch('/api/communities/{community_id}')
def update_community(community_id:int,data:CommunityUpdate,u=Depends(current_user)):
    c=db(); role=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_community'): c.close(); raise HTTPException(403,'Você não tem permissão para alterar a comunidade')
    c.execute('UPDATE communities SET name=?,description=?,icon=? WHERE id=?',(data.name.strip(),data.description.strip(),data.icon.strip(),community_id)); c.commit(); r=c.execute('SELECT * FROM communities WHERE id=?',(community_id,)).fetchone(); c.close(); return dict(r)

@app.patch('/api/communities/{community_id}/settings')
def patch_community_settings(community_id:int,data:SettingsIn,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_community'): c.close(); raise HTTPException(403,'Sem permissão para configurar a comunidade')
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone(); current=json.loads(row['settings'] or '{}')
    current.update({k:v for k,v in data.settings.items() if k in COMMUNITY_SETTING_KEYS})
    c.execute('UPDATE communities SET settings=? WHERE id=?',(json.dumps(current,ensure_ascii=False),community_id)); c.commit(); c.close(); return {'ok':True,'settings':current}

@app.delete('/api/communities/{community_id}')
def delete_community(community_id:int,u=Depends(current_user)):
    c=db(); co=c.execute('SELECT owner_id FROM communities WHERE id=?',(community_id,)).fetchone()
    if not co: c.close(); raise HTTPException(404,'Comunidade não encontrada')
    if co['owner_id']!=u['id']: c.close(); raise HTTPException(403,'Somente o dono pode excluir a comunidade')
    c.execute('DELETE FROM communities WHERE id=?',(community_id,)); c.commit(); c.close(); return {'ok':True}

@app.post('/api/communities/{community_id}/folders')
def create_folder(community_id:int, data:dict, u=Depends(current_user)):
    name=str(data.get('name','')).strip()[:50]
    if not name: raise HTTPException(400,'Informe um nome para a pasta')
    c=db(); require_permission(c,community_id,u['id'],'manage_channels','Somente a administração pode criar pastas')
    pos=c.execute('SELECT COALESCE(MAX(position),-1)+1 FROM community_folders WHERE community_id=?',(community_id,)).fetchone()[0]
    cur=c.execute('INSERT INTO community_folders(community_id,name,position,created_at) VALUES(?,?,?,?)',(community_id,name,pos,int(time.time())))
    c.commit(); row=dict(c.execute('SELECT * FROM community_folders WHERE id=?',(cur.lastrowid,)).fetchone()); c.close(); return row

@app.patch('/api/communities/{community_id}/folders/{folder_id}')
def update_folder(community_id:int,folder_id:int,data:dict,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'manage_channels','Sem permissão para editar pastas')
    row=c.execute('SELECT * FROM community_folders WHERE id=? AND community_id=?',(folder_id,community_id)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Pasta não encontrada')
    if 'name' in data:
        name=str(data['name']).strip()[:50]
        if not name: c.close(); raise HTTPException(400,'O nome não pode ficar vazio')
        c.execute('UPDATE community_folders SET name=? WHERE id=?',(name,folder_id))
    if 'position' in data: c.execute('UPDATE community_folders SET position=? WHERE id=?',(max(0,int(data['position'])),folder_id))
    c.commit(); out=dict(c.execute('SELECT * FROM community_folders WHERE id=?',(folder_id,)).fetchone()); c.close(); return out

@app.delete('/api/communities/{community_id}/folders/{folder_id}')
def delete_folder(community_id:int,folder_id:int,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'manage_channels','Sem permissão para excluir pastas')
    row=c.execute('SELECT id FROM community_folders WHERE id=? AND community_id=?',(folder_id,community_id)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Pasta não encontrada')
    c.execute('UPDATE channels SET folder_id=NULL WHERE community_id=? AND folder_id=?',(community_id,folder_id)); c.execute('DELETE FROM community_folders WHERE id=?',(folder_id,)); c.commit(); c.close(); return {'ok':True}

@app.patch('/api/channels/{channel_id}/folder')
def move_channel_folder(channel_id:int,data:dict,u=Depends(current_user)):
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,'Canal não encontrado')
    require_permission(c,ch['community_id'],u['id'],'manage_channels','Sem permissão para mover canais')
    folder_id=data.get('folder_id')
    if folder_id is not None and not c.execute('SELECT 1 FROM community_folders WHERE id=? AND community_id=?',(int(folder_id),ch['community_id'])).fetchone(): c.close(); raise HTTPException(400,'Pasta inválida')
    c.execute('UPDATE channels SET folder_id=?,position=? WHERE id=?',(int(folder_id) if folder_id is not None else None,max(0,int(data.get('position',0))),channel_id)); c.commit(); c.close(); return {'ok':True}

@app.patch('/api/communities/{community_id}/showcase')
def update_showcase(community_id:int,data:dict,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'manage_community','Somente a administração pode editar a vitrine')
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone(); settings=json.loads(row['settings'] or '{}')
    for key,limit in [('showcase_title',80),('showcase_description',500),('showcase_banner_url',500)]:
        if key in data: settings[key]=str(data[key] or '').strip()[:limit]
    c.execute('UPDATE communities SET settings=? WHERE id=?',(json.dumps(settings,ensure_ascii=False),community_id)); c.commit(); c.close(); return {'ok':True,'settings':settings}

@app.get('/api/communities/{community_id}/events')
def list_events(community_id:int,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id']); rows=c.execute('SELECT e.*,u.display_name AS creator_name FROM community_events e JOIN users u ON u.id=e.created_by WHERE e.community_id=? ORDER BY e.starts_at',(community_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/api/communities/{community_id}/events')
def create_event(community_id:int,data:dict,u=Depends(current_user)):
    title=str(data.get('title','')).strip()[:100]; starts=str(data.get('starts_at','')).strip()
    if not title or not starts: raise HTTPException(400,'Informe o título e a data do evento')
    c=db(); require_permission(c,community_id,u['id'],'manage_community','Somente a administração pode criar eventos')
    cur=c.execute('INSERT INTO community_events(community_id,title,description,starts_at,location,created_by,created_at) VALUES(?,?,?,?,?,?,?)',(community_id,title,str(data.get('description',''))[:1000],starts,str(data.get('location',''))[:160],u['id'],int(time.time())))
    c.commit(); out=dict(c.execute('SELECT * FROM community_events WHERE id=?',(cur.lastrowid,)).fetchone()); c.close(); return out

@app.delete('/api/communities/{community_id}/events/{event_id}')
def delete_event(community_id:int,event_id:int,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'manage_community','Somente a administração pode excluir eventos')
    c.execute('DELETE FROM community_events WHERE id=? AND community_id=?',(event_id,community_id)); c.commit(); c.close(); return {'ok':True}

@app.get('/api/communities/{community_id}/gallery')
def list_gallery(community_id:int,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id']); rows=c.execute('SELECT g.*,u.display_name AS creator_name FROM gallery_items g JOIN users u ON u.id=g.created_by WHERE g.community_id=? ORDER BY g.id DESC',(community_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/api/communities/{community_id}/gallery')
def add_gallery_item(community_id:int,data:dict,u=Depends(current_user)):
    title=str(data.get('title','')).strip()[:100]; media_url=str(data.get('media_url','')).strip()[:500]
    if not title or not media_url.startswith(('https://','http://')): raise HTTPException(400,'Informe um título e um link http(s) válido para a criação')
    c=db(); require_member(c,community_id,u['id']); cur=c.execute('INSERT INTO gallery_items(community_id,title,description,media_url,created_by,created_at) VALUES(?,?,?,?,?,?)',(community_id,title,str(data.get('description',''))[:1000],media_url,u['id'],int(time.time())))
    c.commit(); out=dict(c.execute('SELECT * FROM gallery_items WHERE id=?',(cur.lastrowid,)).fetchone()); c.close(); return out

@app.delete('/api/communities/{community_id}/gallery/{item_id}')
def delete_gallery_item(community_id:int,item_id:int,u=Depends(current_user)):
    c=db(); row=c.execute('SELECT created_by FROM gallery_items WHERE id=? AND community_id=?',(item_id,community_id)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Item não encontrado')
    role=require_member(c,community_id,u['id'])
    if row['created_by']!=u['id'] and not has_community_permission(c,community_id,u['id'],'manage_community'): c.close(); raise HTTPException(403,'Você só pode excluir suas próprias publicações')
    c.execute('DELETE FROM gallery_items WHERE id=?',(item_id,)); c.commit(); c.close(); return {'ok':True}

@app.post('/api/communities/{community_id}/channels')
def create_channel(community_id:int,data:ChannelIn,u=Depends(current_user)):
    if data.kind != 'text': raise HTTPException(400,'O Niro Chat oferece apenas canais de texto; chat de voz foi removido.')
    c=db(); role=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_channels'): c.close(); raise HTTPException(403,'Sem permissão para criar canais')
    try: cur=c.execute('INSERT INTO channels(community_id,name,kind,created_at) VALUES(?,?,?,?)',(community_id,data.name,data.kind,int(time.time()))); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(400,'Não foi possível criar canal')
    r=c.execute('SELECT * FROM channels WHERE id=?',(cur.lastrowid,)).fetchone(); c.close(); return dict(r)

@app.patch('/api/channels/{channel_id}/settings')
def patch_channel_settings(channel_id:int,data:SettingsIn,u=Depends(current_user)):
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,'Canal não encontrado')
    require_permission(c,ch['community_id'],u['id'],'manage_channels','Sem permissão para configurar este canal')
    current=json.loads(ch['settings'] or '{}'); current.update({k:v for k,v in data.settings.items() if k in CHANNEL_SETTING_KEYS})
    current['slowmode_seconds']=max(0,min(3600,int(current.get('slowmode_seconds',0) or 0)))
    if current.get('slowmode_role_key','all') not in role_order(c,ch['community_id']): current['slowmode_role_key']='all'
    c.execute('UPDATE channels SET settings=? WHERE id=?',(json.dumps(current,ensure_ascii=False),channel_id)); c.commit(); c.close(); return {'ok':True,'settings':current}

@app.delete('/api/channels/{channel_id}')
def delete_channel(channel_id:int,u=Depends(current_user)):
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,'Canal não encontrado')
    role=require_member(c,ch['community_id'],u['id'])
    if not has_community_permission(c,ch['community_id'],u['id'],'manage_channels'): c.close(); raise HTTPException(403,'Sem permissão')
    count=c.execute('SELECT COUNT(*) n FROM channels WHERE community_id=?',(ch['community_id'],)).fetchone()['n']
    if count<=1: c.close(); raise HTTPException(400,'A comunidade precisa ter ao menos um canal')
    c.execute('DELETE FROM channels WHERE id=?',(channel_id,)); c.commit(); c.close(); return {'ok':True}

@app.get('/api/channels/{channel_id}/messages')
def channel_messages(channel_id:int,u=Depends(current_user)):
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,'Canal não encontrado')
    if not can_view_channel(c,ch,u['id']): c.close(); raise HTTPException(403,'Este canal não está visível para o seu cargo')
    require_permission(c,ch['community_id'],u['id'],'read_messages','Você não tem permissão para ler este canal')
    rows=c.execute('SELECT m.*,u.username,u.display_name,u.avatar FROM messages m JOIN users u ON u.id=m.user_id WHERE channel_id=? ORDER BY id DESC LIMIT 100',(channel_id,)).fetchall(); c.close(); return [dict(r) for r in reversed(rows)]

@app.post('/api/channels/{channel_id}/messages')
def post_message(channel_id:int,data:MessageIn,u=Depends(current_user)):
    content=data.content.strip()
    if not content: raise HTTPException(400,'Mensagem vazia')
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,'Canal não encontrado')
    if not can_view_channel(c,ch,u['id']): c.close(); raise HTTPException(403,'Este canal não está visível para o seu cargo')
    require_permission(c,ch['community_id'],u['id'],'send_messages','Você não tem permissão para enviar mensagens neste canal')
    member=c.execute('SELECT timeout_until FROM community_members WHERE community_id=? AND user_id=?',(ch['community_id'],u['id'])).fetchone()
    if member and int(member['timeout_until'] or 0)>int(time.time()) and not has_community_permission(c,ch['community_id'],u['id'],'manage_members'): c.close(); raise HTTPException(403,'Você está temporariamente impedido de enviar mensagens')
    ch_settings=json.loads(ch['settings'] or '{}')
    if ch_settings.get('read_only') and not has_community_permission(c,ch['community_id'],u['id'],'manage_channels'):
        c.close(); raise HTTPException(403,'Este canal está somente para leitura')
    if ch_settings.get('allow_links') is False and re.search(r'https?://|www\.',content,re.I): c.close(); raise HTTPException(400,'Links não são permitidos neste canal')
    if ch_settings.get('allow_mentions_everyone') is False and re.search(r'@(everyone|here)\b',content,re.I) and not has_community_permission(c,ch['community_id'],u['id'],'manage_channels'): c.close(); raise HTTPException(403,'Menções globais não são permitidas neste canal')
    slow=max(0,min(3600,int(ch_settings.get('slowmode_seconds',0) or 0)))
    if not slow: slow=max(0,min(3600,int(ch_settings.get('default_slowmode',0) or 0)))
    if slow and slowmode_applies(c,ch['community_id'],u['id'],ch_settings):
        last=c.execute('SELECT created_at FROM messages WHERE channel_id=? AND user_id=? ORDER BY id DESC LIMIT 1',(channel_id,u['id'])).fetchone()
        if last and int(time.time())-last['created_at']<slow:
            wait=slow-(int(time.time())-last['created_at']); c.close(); raise HTTPException(429,f'Aguarde {wait} segundos para enviar outra mensagem')
    msg_type=str(getattr(data,'message_type','chat') or 'chat')
    if msg_type not in ('chat','announcement','poll','suggestion'): msg_type='chat'
    cur=c.execute('INSERT INTO messages(channel_id,user_id,content,created_at,message_type) VALUES(?,?,?,?,?)',(channel_id,u['id'],content,int(time.time()),msg_type)); c.commit()
    r=c.execute('SELECT m.*,u.username,u.display_name,u.avatar FROM messages m JOIN users u ON u.id=m.user_id WHERE m.id=?',(cur.lastrowid,)).fetchone(); c.close(); return dict(r)

@app.get('/api/messages/{message_id}/poll')
def poll_results(message_id:int,u=Depends(current_user)):
    c=db(); row=c.execute("SELECT m.content,m.channel_id,ch.community_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.message_type='poll'",(message_id,)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Enquete não encontrada')
    require_permission(c,row['community_id'],u['id'],'read_messages','Sem permissão para ver esta enquete')
    try: poll=json.loads(row['content']); options=poll.get('options',[])
    except Exception: c.close(); raise HTTPException(400,'Enquete inválida')
    votes=[0]*len(options)
    for vote in c.execute('SELECT option_index,COUNT(*) n FROM poll_votes WHERE message_id=? GROUP BY option_index',(message_id,)).fetchall():
        if 0<=vote['option_index']<len(votes): votes[vote['option_index']]=vote['n']
    mine=c.execute('SELECT option_index FROM poll_votes WHERE message_id=? AND user_id=?',(message_id,u['id'])).fetchone(); c.close()
    return {'options':options,'votes':votes,'my_vote':mine['option_index'] if mine else None,'total':sum(votes)}

@app.post('/api/messages/{message_id}/poll/vote')
def vote_poll(message_id:int,data:dict,u=Depends(current_user)):
    try: idx=int(data.get('option_index',-1))
    except Exception: raise HTTPException(400,'Opção inválida')
    c=db(); row=c.execute("SELECT m.content,ch.community_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.message_type='poll'",(message_id,)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Enquete não encontrada')
    require_permission(c,row['community_id'],u['id'],'read_messages','Sem permissão para votar nesta enquete')
    try: options=json.loads(row['content']).get('options',[])
    except Exception: options=[]
    if idx<0 or idx>=len(options): c.close(); raise HTTPException(400,'Opção inválida')
    c.execute('INSERT INTO poll_votes(message_id,user_id,option_index,voted_at) VALUES(?,?,?,?) ON CONFLICT(message_id,user_id) DO UPDATE SET option_index=excluded.option_index,voted_at=excluded.voted_at',(message_id,u['id'],idx,int(time.time()))); c.commit(); c.close()
    return poll_results(message_id,u)

@app.patch('/api/communities/{community_id}/members/{member_id}/role')
def set_member_role(community_id:int,member_id:int,data:RoleIn,u=Depends(current_user)):
    allowed=('Member','Moderator','Admin')
    if data.role not in allowed: raise HTTPException(400,'Cargo inválido')
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_members'): c.close(); raise HTTPException(403,'Sem permissão para alterar cargos principais')
    target=member_role(c,community_id,member_id)
    if not target: c.close(); raise HTTPException(404,'Membro não encontrado')
    if target['role']=='Owner' or not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Você não pode alterar este membro por causa da hierarquia de cargos')
    if data.role=='Owner': c.close(); raise HTTPException(400,'O cargo Dono não pode ser atribuído manualmente')
    order=role_order(c,community_id); actor_rank=role_rank(c,community_id,u['id']); new_key=f'builtin:{data.role}'
    if new_key not in order or order.index(new_key)<=actor_rank: c.close(); raise HTTPException(403,'Você não pode atribuir um cargo igual ou superior ao seu')
    c.execute('UPDATE community_members SET role=? WHERE community_id=? AND user_id=?',(data.role,community_id,member_id)); c.commit(); c.close(); return {'ok':True}

@app.delete('/api/communities/{community_id}/members/{member_id}')
def remove_member(community_id:int,member_id:int,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id']); target=member_role(c,community_id,member_id)
    if not target: c.close(); raise HTTPException(404,'Membro não encontrado')
    if member_id==u['id'] and actor!='Owner': c.execute('DELETE FROM community_members WHERE community_id=? AND user_id=?',(community_id,u['id'])); c.commit(); c.close(); return {'ok':True}
    if not (has_community_permission(c,community_id,u['id'],'kick_members') or has_community_permission(c,community_id,u['id'],'manage_members')) or target['role']=='Owner' or not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Sem permissão para remover este membro pela hierarquia de cargos')
    c.execute('DELETE FROM community_members WHERE community_id=? AND user_id=?',(community_id,member_id)); c.commit(); c.close(); return {'ok':True}

@app.delete('/api/messages/{message_id}')
async def delete_message(message_id:int,u=Depends(current_user)):
    c=db(); row=c.execute('SELECT m.user_id,m.channel_id,ch.community_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(message_id,)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Mensagem não encontrada')
    require_member(c,row['community_id'],u['id'])
    if row['user_id']!=u['id'] and not has_community_permission(c,row['community_id'],u['id'],'delete_messages'):
        c.close(); raise HTTPException(403,'Sem permissão para apagar mensagens de outras pessoas')
    if row['user_id']==u['id'] and not has_community_permission(c,row['community_id'],u['id'],'read_messages'):
        c.close(); raise HTTPException(403,'Sem permissão')
    c.execute('DELETE FROM messages WHERE id=?',(message_id,)); c.commit(); channel_id=row['channel_id']; c.close()
    try: asyncio.create_task(hub.broadcast(channel_id,{'deleted_id':message_id}))
    except Exception: pass
    return {'ok':True}

@app.post('/api/communities/{community_id}/members/{member_id}/timeout')
def timeout_member(community_id:int,member_id:int,data:dict,u=Depends(current_user)):
    try: minutes=max(1,min(10080,int(data.get('minutes',10))))
    except Exception: raise HTTPException(400,'Duração inválida')
    c=db(); require_member(c,community_id,u['id'])
    target=member_role(c,community_id,member_id)
    if not target: c.close(); raise HTTPException(404,'Membro não encontrado')
    if not has_community_permission(c,community_id,u['id'],'timeout_members') or not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Você não pode castigar este membro')
    until=int(time.time())+minutes*60
    c.execute('UPDATE community_members SET timeout_until=? WHERE community_id=? AND user_id=?',(until,community_id,member_id)); c.commit(); c.close(); return {'ok':True,'timeout_until':until}

@app.post('/api/communities/{community_id}/members/{member_id}/ban')
def ban_member(community_id:int,member_id:int,data:dict,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id'])
    target=member_role(c,community_id,member_id)
    if not target: c.close(); raise HTTPException(404,'Membro não encontrado')
    if not has_community_permission(c,community_id,u['id'],'ban_members') or not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Você não pode banir este membro')
    reason=str(data.get('reason',''))[:300]
    c.execute('INSERT OR REPLACE INTO community_bans(community_id,user_id,banned_by,reason,created_at) VALUES(?,?,?,?,?)',(community_id,member_id,u['id'],reason,int(time.time())))
    c.execute('DELETE FROM community_members WHERE community_id=? AND user_id=?',(community_id,member_id)); c.commit(); c.close(); return {'ok':True}

@app.get('/api/communities/{community_id}/bans')
def list_bans(community_id:int,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'ban_members','Sem permissão para ver os banimentos')
    rows=c.execute('SELECT b.user_id,b.reason,b.created_at,u.username,u.display_name FROM community_bans b JOIN users u ON u.id=b.user_id WHERE b.community_id=? ORDER BY b.created_at DESC',(community_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.delete('/api/communities/{community_id}/bans/{member_id}')
def unban_member(community_id:int,member_id:int,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'ban_members','Sem permissão para remover banimentos')
    cur=c.execute('DELETE FROM community_bans WHERE community_id=? AND user_id=?',(community_id,member_id)); c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,'Banimento não encontrado')
    return {'ok':True}

@app.get('/api/communities/{community_id}/invite')
def get_invite(community_id:int,u=Depends(current_user)):
    c=db(); role=require_member(c,community_id,u['id']); co=c.execute('SELECT invite_code,settings FROM communities WHERE id=?',(community_id,)).fetchone(); settings=json.loads(co['settings'] or '{}')
    if settings.get('allow_member_invites',True) is False and role not in ('Owner','Admin'):
        c.close(); raise HTTPException(403,'Somente a administração pode criar convites nesta comunidade')
    c.close(); return {'invite_code':co['invite_code']}

@app.get('/api/communities/{community_id}/roles')
def list_roles(community_id:int,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id'])
    rows=c.execute('SELECT * FROM community_roles WHERE community_id=? ORDER BY position DESC,id',(community_id,)).fetchall()
    out=[]
    for r in rows:
        x=dict(r); x['permissions']=json.loads(x['permissions']); x['settings']=json.loads(x.get('settings') or '{}'); out.append(x)
    c.close(); return out


@app.post('/api/communities/{community_id}/roles')
def create_role(community_id:int,data:CustomRoleIn,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_roles'): c.close(); raise HTTPException(403,'Sem permissão para criar cargos')
    allowed={'manage_channels','manage_roles','manage_members','manage_community','send_messages','read_messages','create_invites','manage_messages','delete_messages','timeout_members','kick_members','ban_members','pin_messages','bypass_slowmode'}
    perms={k:bool(v) for k,v in data.permissions.items() if k in allowed}
    try:
        pos=c.execute('SELECT COALESCE(MAX(position),0)+1 FROM community_roles WHERE community_id=?',(community_id,)).fetchone()[0]
        cur=c.execute('INSERT INTO community_roles(community_id,name,color,permissions,position,created_at) VALUES(?,?,?,?,?,?)',(community_id,data.name.strip(),data.color,json.dumps(perms),pos,int(time.time())))
        c.commit(); r=c.execute('SELECT * FROM community_roles WHERE id=?',(cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(409,'Já existe um cargo com esse nome')
    c.close(); out=dict(r); out['permissions']=json.loads(out['permissions']); out['settings']=json.loads(out.get('settings') or '{}'); return out

@app.patch('/api/communities/{community_id}/roles/hierarchy')
def update_role_hierarchy(community_id:int,data:dict,u=Depends(current_user)):
    c=db(); require_member(c,community_id,u['id'])
    actor=member_role(c,community_id,u['id'])
    if not actor or actor['role']!='Owner':
        c.close(); raise HTTPException(403,'Somente o dono pode configurar a hierarquia e as permissões globais')
    row=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone(); settings=json.loads(row['settings'] or '{}')
    valid=set(role_order(c,community_id))
    requested=data.get('order',[])
    if not isinstance(requested,list): c.close(); raise HTTPException(400,'A ordem dos cargos é inválida')
    order=[]
    for key in requested:
        key=str(key)
        if key in valid and key not in order: order.append(key)
    order=[x for x in order if x!='builtin:Owner']
    order.insert(0,'builtin:Owner')
    if 'builtin:Member' in order: order.remove('builtin:Member')
    order.append('builtin:Member')
    order += [x for x in valid if x not in order]
    matrix=settings.get('role_permission_matrix',{})
    incoming=data.get('permissions',{})
    if isinstance(incoming,dict):
        for key,perms in incoming.items():
            if key in valid and key!='builtin:Owner' and isinstance(perms,dict):
                matrix[key]={p:bool(perms.get(p,False)) for p in PERMISSION_KEYS}
    settings['role_order']=order; settings['role_permission_matrix']=matrix
    c.execute('UPDATE communities SET settings=? WHERE id=?',(json.dumps(settings,ensure_ascii=False),community_id))
    for pos,key in enumerate(reversed(order)):
        if key.startswith('custom:'):
            try: c.execute('UPDATE community_roles SET position=? WHERE id=? AND community_id=?',(pos,int(key.split(':',1)[1]),community_id))
            except ValueError: pass
    c.commit(); c.close(); return {'ok':True,'order':order,'permissions':matrix}


@app.patch('/api/communities/{community_id}/roles/{role_id}')
def update_role(community_id:int,role_id:int,data:CustomRoleIn,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_roles'): c.close(); raise HTTPException(403,'Sem permissão para editar cargos personalizados')
    role=c.execute('SELECT * FROM community_roles WHERE id=? AND community_id=?',(role_id,community_id)).fetchone()
    if not role: c.close(); raise HTTPException(404,'Cargo não encontrado')
    if not can_manage_role(c,community_id,u['id'],role_id): c.close(); raise HTTPException(403,'Você não pode alterar um cargo de nível igual ou superior ao seu')
    allowed={'manage_channels','manage_roles','manage_members','manage_community','send_messages','read_messages','create_invites','manage_messages','delete_messages','timeout_members','kick_members','ban_members','pin_messages','bypass_slowmode'}
    perms={k:bool(v) for k,v in data.permissions.items() if k in allowed}
    try:
        c.execute('UPDATE community_roles SET name=?,color=?,permissions=? WHERE id=?',(data.name.strip(),data.color,json.dumps(perms),role_id))
        co=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone(); co_settings=json.loads(co['settings'] or '{}'); matrix=co_settings.get('role_permission_matrix',{})
        if f'custom:{role_id}' in matrix: matrix[f'custom:{role_id}'].update({k:bool(v) for k,v in perms.items()}); co_settings['role_permission_matrix']=matrix; c.execute('UPDATE communities SET settings=? WHERE id=?',(json.dumps(co_settings,ensure_ascii=False),community_id))
        c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,'Já existe um cargo com esse nome')
    r=c.execute('SELECT * FROM community_roles WHERE id=?',(role_id,)).fetchone(); c.close(); out=dict(r); out['permissions']=json.loads(out['permissions']); out['settings']=json.loads(out.get('settings') or '{}'); return out

@app.patch('/api/communities/{community_id}/roles/{role_id}/settings')
def patch_role_settings(community_id:int,role_id:int,data:SettingsIn,u=Depends(current_user)):
    c=db(); require_permission(c,community_id,u['id'],'manage_roles','Sem permissão para configurar cargos')
    row=c.execute('SELECT settings FROM community_roles WHERE id=? AND community_id=?',(role_id,community_id)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Cargo não encontrado')
    if not can_manage_role(c,community_id,u['id'],role_id): c.close(); raise HTTPException(403,'Você não pode configurar um cargo de nível igual ou superior ao seu')
    current=json.loads(row['settings'] or '{}'); current.update({k:v for k,v in data.settings.items() if k in ROLE_SETTING_KEYS})
    role_row=c.execute('SELECT permissions FROM community_roles WHERE id=? AND community_id=?',(role_id,community_id)).fetchone()
    permissions=json.loads(role_row['permissions'] or '{}')
    mapping={'can_create_invites':'create_invites','can_manage_channel_settings':'manage_channels','can_manage_profile':'manage_community','can_kick_members':'kick_members','can_manage_messages':'manage_messages','can_delete_messages':'delete_messages','can_timeout_members':'timeout_members','can_ban_members':'ban_members','can_pin_messages':'pin_messages','can_bypass_slowmode':'bypass_slowmode'}
    for setting_key,permission_key in mapping.items():
        if setting_key in current: permissions[permission_key]=bool(current[setting_key])
    c.execute('UPDATE community_roles SET settings=?,permissions=? WHERE id=?',(json.dumps(current,ensure_ascii=False),json.dumps(permissions),role_id))
    co=c.execute('SELECT settings FROM communities WHERE id=?',(community_id,)).fetchone(); co_settings=json.loads(co['settings'] or '{}'); matrix=co_settings.get('role_permission_matrix',{})
    if f'custom:{role_id}' in matrix:
        for sk,pk in mapping.items(): matrix[f'custom:{role_id}'][pk]=bool(current.get(sk,permissions.get(pk,False)))
        co_settings['role_permission_matrix']=matrix; c.execute('UPDATE communities SET settings=? WHERE id=?',(json.dumps(co_settings,ensure_ascii=False),community_id))
    c.commit(); c.close(); return {'ok':True,'settings':current,'permissions':permissions}

@app.delete('/api/communities/{community_id}/roles/{role_id}')
def delete_role(community_id:int,role_id:int,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_roles'): c.close(); raise HTTPException(403,'Sem permissão para excluir cargos personalizados')
    if not can_manage_role(c,community_id,u['id'],role_id): c.close(); raise HTTPException(403,'Você não pode excluir um cargo de nível igual ou superior ao seu')
    cur=c.execute('DELETE FROM community_roles WHERE id=? AND community_id=?',(role_id,community_id)); c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,'Cargo não encontrado')
    return {'ok':True}

@app.put('/api/communities/{community_id}/members/{member_id}/roles')
def assign_custom_role(community_id:int,member_id:int,data:AssignRoleIn,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_roles'): c.close(); raise HTTPException(403,'Sem permissão para atribuir cargos')
    if not member_role(c,community_id,member_id): c.close(); raise HTTPException(404,'Membro não encontrado')
    if not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Você não pode alterar os cargos deste membro pela hierarquia')
    role=c.execute('SELECT id FROM community_roles WHERE id=? AND community_id=?',(data.role_id,community_id)).fetchone()
    if not role: c.close(); raise HTTPException(404,'Cargo personalizado não encontrado')
    order=role_order(c,community_id); actor_rank=role_rank(c,community_id,u['id']); target_role_key=f'custom:{data.role_id}'
    if target_role_key not in order or order.index(target_role_key)<=actor_rank: c.close(); raise HTTPException(403,'Você não pode atribuir um cargo igual ou superior ao seu')
    try: c.execute('INSERT INTO member_custom_roles(community_id,user_id,role_id,assigned_at) VALUES(?,?,?,?)',(community_id,member_id,data.role_id,int(time.time()))); c.commit()
    except sqlite3.IntegrityError: pass
    c.close(); return {'ok':True}

@app.delete('/api/communities/{community_id}/members/{member_id}/roles/{role_id}')
def unassign_custom_role(community_id:int,member_id:int,role_id:int,u=Depends(current_user)):
    c=db(); actor=require_member(c,community_id,u['id'])
    if not has_community_permission(c,community_id,u['id'],'manage_roles'): c.close(); raise HTTPException(403,'Sem permissão para remover cargos')
    if not can_manage_target(c,community_id,u['id'],member_id): c.close(); raise HTTPException(403,'Você não pode alterar os cargos deste membro pela hierarquia')
    c.execute('DELETE FROM member_custom_roles WHERE community_id=? AND user_id=? AND role_id=?',(community_id,member_id,role_id)); c.commit(); c.close(); return {'ok':True}

class Hub:
    def __init__(self): self.connections={}
    async def connect(self,channel_id,ws):
        await ws.accept(); self.connections.setdefault(channel_id,set()).add(ws)
    def disconnect(self,channel_id,ws):
        self.connections.get(channel_id,set()).discard(ws)
    async def broadcast(self,channel_id,payload):
        for ws in list(self.connections.get(channel_id,set())):
            try: await ws.send_json(payload)
            except Exception: self.disconnect(channel_id,ws)
hub=Hub()

@app.websocket('/ws/channels/{channel_id}')
async def channel_ws(ws:WebSocket,channel_id:int,token:str):
    origin = (ws.headers.get('origin') or '').rstrip('/')
    if ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    try: u=token_user(token)
    except Exception: await ws.close(code=4401); return
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch or not member_role(c,ch['community_id'],u['id']) or not can_view_channel(c,ch,u['id']) or not has_community_permission(c,ch['community_id'],u['id'],'read_messages'): c.close(); await ws.close(code=4403); return
    c.close(); await hub.connect(channel_id,ws)
    try:
        while True:
            data=await ws.receive_json(); content=str(data.get('content','')).strip(); msg_type=str(data.get('message_type','chat') or 'chat')
            if msg_type not in ('chat','announcement','poll','suggestion'): msg_type='chat'
            if not content or len(content)>4000: continue
            c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
            if not ch or not can_view_channel(c,ch,u['id']) or not has_community_permission(c,ch['community_id'],u['id'],'send_messages'):
                c.close(); await ws.send_json({'error':'Você não tem permissão para enviar mensagens neste canal'}); continue
            member=c.execute('SELECT timeout_until FROM community_members WHERE community_id=? AND user_id=?',(ch['community_id'],u['id'])).fetchone()
            if member and int(member['timeout_until'] or 0)>int(time.time()) and not has_community_permission(c,ch['community_id'],u['id'],'manage_members'):
                c.close(); await ws.send_json({'error':'Você está temporariamente impedido de enviar mensagens'}); continue
            ch_settings=json.loads(ch['settings'] or '{}')
            if ch_settings.get('read_only') and not has_community_permission(c,ch['community_id'],u['id'],'manage_channels'):
                c.close(); await ws.send_json({'error':'Este canal está somente para leitura'}); continue
            if ch_settings.get('allow_links') is False and re.search(r'https?://|www\.',content,re.I):
                c.close(); await ws.send_json({'error':'Links não são permitidos neste canal'}); continue
            if ch_settings.get('allow_mentions_everyone') is False and re.search(r'@(everyone|here)\b',content,re.I) and not has_community_permission(c,ch['community_id'],u['id'],'manage_channels'):
                c.close(); await ws.send_json({'error':'Menções globais não são permitidas neste canal'}); continue
            slow=max(0,min(3600,int(ch_settings.get('slowmode_seconds',0) or 0)))
            if not slow: slow=max(0,min(3600,int(ch_settings.get('default_slowmode',0) or 0)))
            if slow and slowmode_applies(c,ch['community_id'],u['id'],ch_settings):
                last=c.execute('SELECT created_at FROM messages WHERE channel_id=? AND user_id=? ORDER BY id DESC LIMIT 1',(channel_id,u['id'])).fetchone()
                if last and int(time.time())-last['created_at']<slow:
                    wait=slow-(int(time.time())-last['created_at']); c.close(); await ws.send_json({'error':f'Aguarde {wait} segundos para enviar outra mensagem'}); continue
            cur=c.execute('INSERT INTO messages(channel_id,user_id,content,created_at,message_type) VALUES(?,?,?,?,?)',(channel_id,u['id'],content,int(time.time()),msg_type)); c.commit(); r=c.execute('SELECT m.*,u.username,u.display_name,u.avatar FROM messages m JOIN users u ON u.id=m.user_id WHERE m.id=?',(cur.lastrowid,)).fetchone(); c.close()
            await hub.broadcast(channel_id,dict(r))
    except WebSocketDisconnect: hub.disconnect(channel_id,ws)
    except Exception: hub.disconnect(channel_id,ws)
