import os
import json
import re
import atexit
import random
import secrets
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template, redirect, url_for, flash, request, abort, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))
load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

if not os.environ.get("SECRET_KEY"):
    logging.warning(
        "SECRET_KEY is not set. Sessions will reset whenever the app restarts. "
        "Set SECRET_KEY in your .env file."
    )

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///eventops.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY not set. The AI assistant will not work.")

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.unauthorized_handler
def unauthorized_callback():
    if (
        request.path.startswith("/api/")
        or request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ):
        return jsonify({
            "error": "Your session has expired. Please log in again to continue.",
            "auth_required": True,
        }), 401
    return redirect(url_for("login", next=request.url))


# ============================================================
# DATABASE MODELS
# ============================================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="user")
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    date = db.Column(db.String(50))
    time = db.Column(db.String(50))
    location = db.Column(db.String(200))
    capacity = db.Column(db.Integer)
    status = db.Column(db.String(20), default="upcoming")
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Registration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"))
    ticket_type = db.Column(db.String(50))
    status = db.Column(db.String(20), default="registered")
    checked_in = db.Column(db.Boolean, default=False)
    registered_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship("User", backref="registrations")
    event = db.relationship("Event", backref="registrations")


class Venue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    capacity = db.Column(db.Integer)
    utilization = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="available")
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Speaker(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bio = db.Column(db.Text)
    session_title = db.Column(db.String(200))
    schedule = db.Column(db.String(100))
    availability = db.Column(db.Boolean, default=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Sponsorship(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sponsor_name = db.Column(db.String(100), nullable=False)
    commitment = db.Column(db.String(200))
    deliverables = db.Column(db.String(200))
    visibility_score = db.Column(db.Integer, default=0)
    roi = db.Column(db.Float, default=0.0)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Incident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    priority = db.Column(db.String(20), default="medium")
    status = db.Column(db.String(20), default="open")
    reported_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at = db.Column(db.DateTime, nullable=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    recipient = db.Column(db.String(100))
    channel = db.Column(db.String(20))
    subject = db.Column(db.String(200))
    message = db.Column(db.Text)
    sent_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    read = db.Column(db.Boolean, default=False)

    user = db.relationship("User", backref="notifications")


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False
    )
    event_id = db.Column(
        db.Integer, db.ForeignKey("event.id"), nullable=False
    )
    remind_at = db.Column(db.DateTime, nullable=False)
    message = db.Column(db.Text, nullable=False)
    sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship("User", backref="reminders")
    event = db.relationship("Event", backref="reminders")


class Feedback(db.Model):
    __tablename__ = "feedback"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False
    )
    event_id = db.Column(
        db.Integer, db.ForeignKey("event.id"), nullable=False
    )
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship("User", backref="feedbacks")
    event = db.relationship("Event", backref="feedbacks")


# ============================================================
# LOGIN LOADER AND ADMIN PROTECTION
# ============================================================

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@app.context_processor
def inject_notifications():
    if current_user.is_authenticated:
        query = Notification.query.filter(
            (Notification.user_id == current_user.id) |
            ((Notification.user_id.is_(None)) & (Notification.recipient == current_user.email))
        )
        unread = query.filter_by(read=False).count()
        recent = query.order_by(Notification.sent_at.desc()).limit(3).all()
        return dict(unread_notifications_count=unread, navbar_notifications=recent)
    return dict(unread_notifications_count=0, navbar_notifications=[])


def admin_required(view_function):
    """Require an authenticated administrator."""
    @wraps(view_function)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return view_function(*args, **kwargs)

    return wrapped_view


# ============================================================
# GEMINI AI ASSISTANT
# ============================================================

class GeminiAssistant:
    def __init__(self, user_id):
        self.user_id = user_id

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )

        self.client = genai.Client(api_key=api_key)
        raw_candidates = [
            GEMINI_MODEL,
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-3.5-flash",
            "gemini-2.5-flash",
        ]
        self.candidate_models = []
        for m in raw_candidates:
            if m and m not in self.candidate_models:
                self.candidate_models.append(m)

        self.system_instruction = """
You are EventOps AI, an intelligent assistant for event management.
Help users with events, registrations, venues, speakers, and reminders.

The user is already authenticated. Never ask for their user ID.
Use the authenticated user ID supplied by the application for registrations
and reminders. Never trust a user ID proposed by the model.

Return one JSON object with:
- action
- parameters
- message

Allowed actions:
get_events, get_my_registrations, get_event_details,
schedule_reminder, get_venues, get_speakers, answer

Do not include Markdown fences or extra text around the JSON.
Be friendly and helpful.
"""

        self.functions = {
            "get_events": self.get_events,
            "get_my_registrations": self.get_my_registrations,
            "get_event_details": self.get_event_details,
            "schedule_reminder": self.schedule_reminder,
            "get_venues": self.get_venues,
            "get_speakers": self.get_speakers,
        }

    def get_events(self, **kwargs):
        events = Event.query.order_by(Event.date.asc()).all()
        return [
            {
                "id": event.id,
                "title": event.title,
                "date": event.date,
                "time": event.time,
                "location": event.location,
                "capacity": event.capacity,
                "status": event.status,
            }
            for event in events
        ]

    def get_my_registrations(self, user_id=None, **kwargs):
        uid = user_id or self.user_id
        registrations = Registration.query.filter_by(
            user_id=uid
        ).all()

        return [
            {
                "event_id": registration.event_id,
                "event_title": registration.event.title,
                "ticket_type": registration.ticket_type,
                "checked_in": registration.checked_in,
            }
            for registration in registrations
        ]

    def get_event_details(self, event_id, **kwargs):
        event = db.session.get(Event, event_id)

        if not event:
            return {"error": "Event not found"}

        return {
            "id": event.id,
            "title": event.title,
            "description": event.description,
            "date": event.date,
            "time": event.time,
            "location": event.location,
            "capacity": event.capacity,
            "status": event.status,
        }

    def get_venues(self, **kwargs):
        venues = Venue.query.all()
        return [
            {
                "id": venue.id,
                "name": venue.name,
                "capacity": venue.capacity,
                "utilization": venue.utilization,
                "status": venue.status,
            }
            for venue in venues
        ]

    def get_speakers(self, **kwargs):
        speakers = Speaker.query.all()
        return [
            {
                "id": speaker.id,
                "name": speaker.name,
                "session_title": speaker.session_title,
                "schedule": speaker.schedule,
                "availability": speaker.availability,
            }
            for speaker in speakers
        ]

    def schedule_reminder(
        self, user_id, event_id, remind_at, message
    ):
        try:
            event = db.session.get(Event, event_id)
            user = db.session.get(User, user_id)

            if not event:
                return {"error": "Event not found"}

            if not user or user.id != self.user_id:
                return {"error": "Invalid user"}

            # Flexible parsing of remind_at
            if isinstance(remind_at, str):
                text = remind_at.strip().lower()
                parsed_dt = None
                try:
                    dt = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    parsed_dt = dt
                except Exception:
                    pass

                if not parsed_dt:
                    now = datetime.now(timezone.utc)
                    event_dt = None
                    if event and event.date and event.time:
                        for fmt in ["%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M%p"]:
                            try:
                                dt_naive = datetime.strptime(f"{event.date} {event.time}".strip(), fmt)
                                event_dt = dt_naive.replace(tzinfo=timezone.utc)
                                break
                            except Exception:
                                continue

                    if "before" in text or "prior" in text or "ahead" in text:
                        h_match = re.search(r"(\d+)\s*(?:hour|hr|h)", text)
                        m_match = re.search(r"(\d+)\s*(?:minute|min|m)", text)
                        hours = int(h_match.group(1)) if h_match else 1
                        mins = int(m_match.group(1)) if m_match else 0
                        delta = timedelta(hours=hours, minutes=mins)
                        parsed_dt = (event_dt - delta) if event_dt else (now + timedelta(hours=1))
                    elif "in " in text or "later" in text:
                        h_match = re.search(r"(\d+)\s*(?:hour|hr|h)", text)
                        m_match = re.search(r"(\d+)\s*(?:minute|min|m)", text)
                        hours = int(h_match.group(1)) if h_match else 0
                        mins = int(m_match.group(1)) if m_match else 0
                        parsed_dt = now + timedelta(hours=hours, minutes=mins)
                    else:
                        parsed_dt = (event_dt - timedelta(hours=1)) if event_dt else (now + timedelta(hours=1))

                remind_at = parsed_dt

            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)

            if not message or not str(message).strip():
                return {"error": "A reminder message is required"}

            reminder = Reminder(
                user_id=self.user_id,
                event_id=event_id,
                remind_at=remind_at,
                message=str(message).strip(),
                sent=False,
            )

            db.session.add(reminder)
            db.session.commit()

            return {
                "success": True,
                "reminder_id": reminder.id,
                "scheduled_for": remind_at.isoformat(),
            }

        except Exception as error:
            db.session.rollback()
            app.logger.exception("Could not schedule reminder")
            return {"error": str(error)}

    def process_query(self, user_message):
        message = (user_message or "").strip()

        if not message:
            return "Please enter a message."

        if re.match(r"^(hi|hello|hey|help|who are you|what can you do)[\s!.]*$", message, re.IGNORECASE):
            return (
                "Hello! I am **EventOps AI**, your intelligent event assistant.\n\n"
                "I can help you with:\n"
                "• **Events**: Find upcoming events and schedules\n"
                "• **Registrations**: Check your registered tickets and status\n"
                "• **Venues**: View hall capacity and availability\n"
                "• **Speakers**: View speaker profiles and sessions\n"
                "• **Reminders**: Schedule event reminders\n\n"
                "How can I assist you today?"
            )

        # Handle common requests directly without requiring Gemini.
        if re.search(
            r"\b(my registrations|my events|my tickets|"
            r"what am i registered for|registrations)\b",
            message,
            re.IGNORECASE,
        ):
            result = self.get_my_registrations(self.user_id)

            if not result:
                return "You are not registered for any events."

            return "\n".join(
                f"• {item['event_title']} "
                f"(Ticket: {item['ticket_type']}) - "
                f"Checked in: {'Yes' if item['checked_in'] else 'No'}"
                for item in result
            )

        if re.search(
            r"\b(upcoming events|show events|list events|"
            r"what events are coming)\b",
            message,
            re.IGNORECASE,
        ):
            events = self.get_events()

            if not events:
                return "No events found."

            return "\n".join(
                f"• {event['title']} on {event['date'] or 'TBD'} "
                f"at {event['time'] or 'TBD'} "
                f"({event['location'] or 'Location TBD'})"
                for event in events
            )

        if re.search(r"\b(venues|rooms|locations)\b", message, re.IGNORECASE):
            venues = self.get_venues()

            if not venues:
                return "No venues available."

            return "\n".join(
                f"• {venue['name']} "
                f"(Capacity: {venue['capacity'] or 'Unknown'}) - "
                f"{venue['status']}"
                for venue in venues
            )

        if re.search(
            r"\b(speakers|sessions|who is speaking)\b",
            message,
            re.IGNORECASE,
        ):
            speakers = self.get_speakers()

            if not speakers:
                return "No speakers scheduled."

            return "\n".join(
                f"• {speaker['name']} - "
                f"{speaker['session_title'] or 'Session TBD'} "
                f"({speaker['schedule'] or 'Schedule TBD'})"
                for speaker in speakers
            )

        prompt = f"""
The authenticated user sent this message:
{json.dumps(message)}

Choose the best action from:
get_events, get_my_registrations, get_event_details,
schedule_reminder, get_venues, get_speakers, answer

Return JSON with "action", "parameters", and "message".

For get_event_details, provide event_name or event_id.
For schedule_reminder, provide event_name or event_id,
remind_at as an ISO date/time, and message.
Never ask for a user ID. The application supplies it.
If the intent is unclear, use action "answer".
"""

        response = None
        last_error = None
        for cand_model in self.candidate_models:
            try:
                response = self.client.models.generate_content(
                    model=cand_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_instruction,
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                )
                if response and getattr(response, "text", None):
                    break
            except Exception as e:
                last_error = e
                app.logger.warning("Gemini candidate model %s failed: %s", cand_model, e)
                continue

        if not response:
            app.logger.error("All Gemini candidate models failed. Last error: %s", last_error)
            err_str = str(last_error or "").lower()
            if "quota" in err_str or "429" in err_str or "resourceexhausted" in err_str:
                return "The AI assistant has reached its Gemini API rate limit. Please wait a moment and try again."
            elif "api_key" in err_str or "unauthenticated" in err_str or "permission" in err_str:
                return "Gemini API key verification failed. Please check the GEMINI_API_KEY in your .env file."
            return (
                "The AI assistant is temporarily unable to reach Gemini services. "
                "You can still ask me about your registrations, upcoming events, venues, and speakers!"
            )

        raw = (getattr(response, "text", None) or "").strip()

        if not raw:
            return "The AI assistant returned an empty response. Please try again."

        raw = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            raw,
            flags=re.IGNORECASE | re.MULTILINE,
        ).strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return "I couldn't understand the AI response. Please try again."

            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return "I couldn't understand the AI response. Please try again."

        if not isinstance(parsed, dict):
            return "The AI returned an invalid response. Please try again."

        action = parsed.get("action", "answer")
        params = parsed.get("parameters") or {}

        if not isinstance(params, dict):
            params = {}

        if action == "answer":
            return str(parsed.get("message") or "How can I help with your event?")

        function = self.functions.get(action)

        if not function:
            return f"Sorry, I cannot perform '{action}' yet."

        if action in {"get_my_registrations", "schedule_reminder"}:
            params["user_id"] = self.user_id

        if action in {"get_event_details", "schedule_reminder"}:
            event_name = params.get("event_name")
            event_id = params.get("event_id")

            if event_name:
                event = Event.query.filter(
                    Event.title.ilike(f"%{event_name}%")
                ).first()

                if not event:
                    return (
                        f"I couldn't find an event matching "
                        f"'{event_name}'. Please check the event name."
                    )

                params["event_id"] = event.id

            if "event_name" in params:
                params.pop("event_name", None)

            if not params.get("event_id"):
                return "Please specify which event you mean."

        try:
            result = function(**params)
        except TypeError:
            app.logger.exception("Invalid assistant action parameters")
            return "I couldn't process that request. Please try again."
        except Exception:
            db.session.rollback()
            app.logger.exception("Assistant action failed")
            return "Something went wrong while processing your request."

        if isinstance(result, list):
            if not result:
                return "No results found."

            lines = []
            for item in result:
                if "event_title" in item:
                    lines.append(
                        f"• {item['event_title']} "
                        f"(Ticket: {item.get('ticket_type', 'Standard')}) - "
                        f"Checked in: "
                        f"{'Yes' if item.get('checked_in') else 'No'}"
                    )
                elif "title" in item and "date" in item:
                    lines.append(
                        f"• {item['title']} on {item.get('date') or 'TBD'} "
                        f"at {item.get('time') or 'TBD'} "
                        f"({item.get('location') or 'Location TBD'})"
                    )
                elif "name" in item:
                    lines.append(
                        f"• {item['name']} "
                        f"{item.get('session_title') or ''} "
                        f"{item.get('schedule') or ''}"
                    )
                else:
                    lines.append(str(item))

            return "\n".join(lines)

        if isinstance(result, dict):
            if "error" in result:
                return f"Error: {result['error']}"

            if result.get("success"):
                return (
                    "Reminder scheduled for "
                    f"{result.get('scheduled_for', 'the requested time')}."
                )

            return "\n".join(f"{key}: {value}" for key, value in result.items())

        return str(result)

# ============================================================
# BACKGROUND REMINDER SCHEDULER
# ============================================================

def process_reminders():
    """Convert due reminders into in-app notifications exactly once."""
    with app.app_context():
        try:
            now = datetime.now(timezone.utc)

            reminders = Reminder.query.filter(
                Reminder.remind_at <= now,
                Reminder.sent.is_(False),
            ).all()

            for reminder in reminders:
                user = db.session.get(User, reminder.user_id)

                if user:
                    notification = Notification(
                        user_id=user.id,
                        recipient=user.email,
                        channel="in_app",
                        subject="Event Reminder",
                        message=reminder.message,
                        sent_at=datetime.now(timezone.utc),
                        read=False,
                    )
                    db.session.add(notification)

                reminder.sent = True

            db.session.commit()

        except Exception:
            db.session.rollback()
            app.logger.exception("Reminder processing failed")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    func=process_reminders,
    trigger="interval",
    seconds=60,
    id="process_reminders",
    replace_existing=True,
)

# Avoid starting two schedulers when Flask's debug reloader is enabled.
if (
        os.environ.get("FLASK_DEBUG", "1") != "1"
        or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
):
    scheduler.start()
    atexit.register(
        lambda: scheduler.shutdown(wait=False)
        if scheduler.running
        else None
    )

# ============================================================
# DATABASE INITIALIZATION AND STARTER DATA
# ============================================================

def init_db():
    """Create starter records without duplicating existing data."""

    admin = User.query.filter_by(email="admin@eventops.com").first()
    regular_user = User.query.filter_by(email="user@eventops.com").first()

    if admin is None:
        admin = User(
            name="Admin User",
            email="admin@eventops.com",
            role="admin",
        )
        admin.set_password(
            os.environ.get("DEMO_ADMIN_PASSWORD", "admin123")
        )
        db.session.add(admin)

    if regular_user is None:
        regular_user = User(
            name="Regular User",
            email="user@eventops.com",
            role="user",
        )
        regular_user.set_password(
            os.environ.get("DEMO_USER_PASSWORD", "user123")
        )
        db.session.add(regular_user)

    db.session.flush()

    if Event.query.count() == 0:
        today = datetime.now().date()

        events = [
            Event(
                title="AI Summit 2026",
                description="The future of AI in business",
                date=(today + timedelta(days=14)).isoformat(),
                time="09:00 AM",
                location="Main Hall",
                capacity=200,
                status="upcoming",
            ),
            Event(
                title="Hackathon Night",
                description="24-hour coding challenge",
                date=(today + timedelta(days=21)).isoformat(),
                time="06:00 PM",
                location="Room B",
                capacity=50,
                status="upcoming",
            ),
            Event(
                title="Sponsor Meet & Greet",
                description="Networking with sponsors",
                date=(today + timedelta(days=30)).isoformat(),
                time="11:00 AM",
                location="Exhibition Hall",
                capacity=100,
                status="upcoming",
            ),
        ]

        db.session.add_all(events)
        db.session.flush()

    if Venue.query.count() == 0:
        db.session.add_all(
            [
                Venue(
                    name=name,
                    capacity=capacity,
                    utilization=utilization,
                    status="available",
                )
                for name, capacity, utilization in [
                ("Main Hall", 500, 0.78),
                ("Room A", 100, 0.35),
                ("Room B", 80, 0.62),
                ("Exhibition Hall", 300, 0.70),
                ("Breakout Area", 50, 0.25),
            ]
            ]
        )

    if Speaker.query.count() == 0:
        db.session.add_all(
            [
                Speaker(
                    name="Dr. Smith",
                    bio="AI Researcher",
                    session_title="Keynote: Future of AI",
                    schedule="Day 1 9:00",
                ),
                Speaker(
                    name="Prof. Lee",
                    bio="Data Scientist",
                    session_title="Data Ethics",
                    schedule="Day 1 11:00",
                ),
                Speaker(
                    name="Ms. Jones",
                    bio="Event Manager",
                    session_title="Operational Excellence",
                    schedule="Day 2 10:00",
                ),
            ]
        )

    if Sponsorship.query.count() == 0:
        db.session.add_all(
            [
                Sponsorship(
                    sponsor_name="Acme Corp",
                    commitment="$50k",
                    deliverables="Logo + booth",
                    visibility_score=85,
                    roi=1.2,
                ),
                Sponsorship(
                    sponsor_name="Beta Inc",
                    commitment="$30k",
                    deliverables="Session sponsor",
                    visibility_score=70,
                    roi=0.9,
                ),
                Sponsorship(
                    sponsor_name="Gamma Ltd",
                    commitment="$20k",
                    deliverables="Networking sponsor",
                    visibility_score=60,
                    roi=0.7,
                ),
            ]
        )

    if Incident.query.count() == 0:
        db.session.add_all(
            [
                Incident(
                    title="Wi-Fi outage",
                    description="Main hall connectivity lost",
                    priority="high",
                ),
                Incident(
                    title="Speaker delay",
                    description="Keynote speaker is late",
                    priority="medium",
                ),
                Incident(
                    title="Catering shortage",
                    description="Lunch not enough for attendees",
                    priority="low",
                ),
            ]
        )

    if Notification.query.count() == 0:
        db.session.add_all(
            [
                Notification(
                    recipient="admin@eventops.com",
                    channel="in_app",
                    subject="Welcome",
                    message="Welcome to EventOps AI",
                ),
                Notification(
                    recipient="user@eventops.com",
                    channel="in_app",
                    subject="Event Reminder",
                    message="Your event is coming up",
                ),
            ]
        )

    db.session.commit()

    # Add sample registrations only when there are none.
    if Registration.query.count() == 0:
        admin = User.query.filter_by(
            email="admin@eventops.com"
        ).first()

        regular_user = User.query.filter_by(
            email="user@eventops.com"
        ).first()

        events = Event.query.order_by(Event.id.asc()).limit(3).all()

        if admin and regular_user and events:
            db.session.add(
                Registration(
                    user_id=admin.id,
                    event_id=events[0].id,
                    ticket_type="VIP",
                )
            )

            if len(events) > 1:
                db.session.add(
                    Registration(
                        user_id=regular_user.id,
                        event_id=events[1].id,
                        ticket_type="Standard",
                    )
                )

            db.session.commit()

# ============================================================
# PUBLIC AND AUTHENTICATION ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = (
                request.form.get("email") or ""
        ).strip().lower()

        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user)
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password", "danger")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("index"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # Public sign-up must never grant admin privileges.
        role = "user"

        if not name or not email or len(password) < 8:
            flash(
                "Please provide your name, a valid email, and a password "
                "with at least 8 characters.",
                "danger",
            )

        elif User.query.filter_by(email=email).first():
            flash("Email already registered", "danger")

        else:
            user = User(
                name=name,
                email=email,
                role=role,
            )
            user.set_password(password)

            db.session.add(user)
            db.session.commit()

            flash(
                "Registration successful! Please login.",
                "success",
            )
            return redirect(url_for("login"))

    return render_template("register.html")

# ============================================================
# USER DASHBOARD AND PROFILE
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    stats = {
        "registrations": Registration.query.count(),
        "venues": Venue.query.count(),
        "speakers": Speaker.query.count(),
        "sponsorships": Sponsorship.query.count(),
    }

    user_regs = Registration.query.filter_by(
        user_id=current_user.id
    ).all()

    stats["my_registrations"] = len(user_regs)
    stats["checked_in"] = sum(
        1 for registration in user_regs
        if registration.checked_in
    )
    stats["upcoming_events"] = Event.query.filter_by(
        status="upcoming"
    ).count()

    recent_incidents = Incident.query.order_by(
        Incident.reported_at.desc()
    ).limit(5).all()

    # These activity entries are illustrative, not live telemetry.
    recent_activities = [
        {
            "icon": "person-plus",
            "color": "primary",
            "text": "John Doe registered for AI Summit 2026",
            "time": "5 min ago",
        },
        {
            "icon": "building",
            "color": "success",
            "text": "Main Hall venue utilization updated to 78%",
            "time": "18 min ago",
        },
        {
            "icon": "mic",
            "color": "warning",
            "text": "Dr. Smith's session rescheduled to 2:00 PM",
            "time": "1 hour ago",
        },
        {
            "icon": "trophy",
            "color": "purple",
            "text": "Acme Corp sponsorship deliverables completed",
            "time": "2 hours ago",
        },
        {
            "icon": "exclamation-triangle",
            "color": "danger",
            "text": "Wi-Fi outage incident resolved",
            "time": "3 hours ago",
        },
    ]

    return render_template(
        "dashboard.html",
        stats=stats,
        my_registrations=user_regs,
        recent_activities=recent_activities,
        recent_incidents=recent_incidents,
    )

@app.route("/profile")
@login_required
def profile():
    user_regs = Registration.query.filter_by(
        user_id=current_user.id
    ).all()

    total_regs = len(user_regs)
    checked_in = sum(
        1 for registration in user_regs
        if registration.checked_in
    )

    stats = {
        "total_registrations": total_regs,
        "checked_in": checked_in,
        "pending": total_regs - checked_in,
    }

    return render_template("profile.html", stats=stats)

# ============================================================
# USER EVENT REGISTRATION ROUTES
# ============================================================

@app.route("/user/register", methods=["GET", "POST"])
@login_required
def user_register_event():
    events = Event.query.filter(
        Event.status != "completed"
    ).all()

    if request.method == "POST":
        event_id = request.form.get("event_id", type=int)
        ticket_type = request.form.get(
            "ticket_type", "Standard"
        )

        if not event_id:
            flash("Please select an event", "danger")

        else:
            event = db.session.get(Event, event_id)

            if not event or event.status != "upcoming":
                flash(
                    "That event is unavailable for registration.",
                    "danger",
                )

            elif ticket_type not in {"Standard", "VIP"}:
                flash("Please choose a valid ticket type.", "danger")

            else:
                existing = Registration.query.filter_by(
                    user_id=current_user.id,
                    event_id=event_id,
                ).first()

                if existing:
                    flash(
                        "You are already registered for this event.",
                        "warning",
                    )

                else:
                    registered_count = Registration.query.filter_by(
                        event_id=event_id
                    ).count()

                    if (
                            event.capacity is not None
                            and registered_count >= event.capacity
                    ):
                        flash("Sorry, this event is full.", "danger")

                    else:
                        registration = Registration(
                            user_id=current_user.id,
                            event_id=event_id,
                            ticket_type=ticket_type,
                        )

                        db.session.add(registration)
                        db.session.commit()

                        flash(
                            "Successfully registered for the event!",
                            "success",
                        )
                        return redirect(url_for("dashboard"))

    return render_template(
        "user_register_event.html",
        events=events,
    )

@app.route("/event/<int:event_id>/register", methods=["GET", "POST"])
@login_required
def register_for_event(event_id):
    event = db.session.get(Event, event_id)

    if not event:
        abort(404)

    if request.method == "POST":
        ticket_type = request.form.get(
            "ticket_type", "Standard"
        )

        if event.status != "upcoming":
            flash(
                "This event is not currently open for registration.",
                "danger",
            )

        elif ticket_type not in {"Standard", "VIP"}:
            flash("Please choose a valid ticket type.", "danger")

        else:
            existing = Registration.query.filter_by(
                user_id=current_user.id,
                event_id=event_id,
            ).first()

            if existing:
                flash(
                    "You are already registered for this event.",
                    "warning",
                )

            else:
                registered_count = Registration.query.filter_by(
                    event_id=event_id
                ).count()

                if (
                        event.capacity is not None
                        and registered_count >= event.capacity
                ):
                    flash("Sorry, this event is full.", "danger")

                else:
                    registration = Registration(
                        user_id=current_user.id,
                        event_id=event_id,
                        ticket_type=ticket_type,
                    )

                    db.session.add(registration)
                    db.session.commit()

                    flash(
                        "Successfully registered for the event!",
                        "success",
                    )
                    return redirect(
                        url_for("event_detail", event_id=event_id)
                    )

    return render_template(
        "event_register.html",
        event=event,
    )

@app.route("/user/registrations")
@app.route("/registrations")
@app.route("/my-registrations")
@login_required
def user_my_registrations():
    registrations = Registration.query.filter_by(
        user_id=current_user.id
    ).all()

    return render_template(
        "user_registrations.html",
        registrations=registrations,
    )

@app.route("/my/passes")
@login_required
def my_registrations():
    return redirect(url_for("user_my_registrations"))

@app.route(
    "/user/registrations/cancel/<int:reg_id>",
    methods=["POST"],
)
@login_required
def cancel_registration(reg_id):
    """Cancel only the logged-in user's own registration."""
    registration = db.session.get(Registration, reg_id)

    if not registration:
        abort(404)

    if registration.user_id != current_user.id:
        abort(403)

    event_title = registration.event.title

    db.session.delete(registration)
    db.session.commit()

    flash(
        f'Your registration for "{event_title}" has been cancelled.',
        "success",
    )
    return redirect(url_for("user_my_registrations"))

@app.route(
    "/user/registrations/edit/<int:reg_id>",
    methods=["POST"],
)
@login_required
def edit_registration(reg_id):
    """Change the ticket type for the logged-in user's registration."""
    registration = db.session.get(Registration, reg_id)

    if not registration:
        abort(404)

    if registration.user_id != current_user.id:
        abort(403)

    new_ticket_type = request.form.get("ticket_type")

    if new_ticket_type in {"Standard", "VIP"}:
        registration.ticket_type = new_ticket_type
        db.session.commit()

        flash(
            f"Ticket type updated to {new_ticket_type}.",
            "success",
        )
    else:
        flash("Invalid ticket type.", "danger")

    return redirect(url_for("user_my_registrations"))

@app.route("/events")
@login_required
def browse_events():
    events = Event.query.order_by(Event.date.asc()).all()

    return render_template(
        "browse_events.html",
        events=events,
    )

@app.route("/event/<int:event_id>")
@login_required
def event_detail(event_id):
    event = db.session.get(Event, event_id)

    if not event:
        abort(404)

    registration = Registration.query.filter_by(
        user_id=current_user.id,
        event_id=event_id,
    ).first()

    speakers = Speaker.query.all()
    venue = Venue.query.filter(Venue.name.ilike(f"%{event.location}%")).first() if event.location else None
    attendee_count = Registration.query.filter_by(event_id=event_id).count()
    existing_feedback = Feedback.query.filter_by(user_id=current_user.id, event_id=event_id).first()

    return render_template(
        "event_detail.html",
        event=event,
        registered=(registration is not None),
        registration=registration,
        speakers=speakers,
        venue=venue,
        attendee_count=attendee_count,
        existing_feedback=existing_feedback,
    )

# ============================================================
# ASSISTANT ROUTES
# ============================================================

@app.route("/assistant")
@login_required
def assistant_page():
    return render_template(
        "assistant.html",
        now=datetime.now(timezone.utc),
    )

@app.route("/api/assistant", methods=["POST"])
def assistant_api():
    if not current_user.is_authenticated:
        return jsonify({
            "error": "Your session has expired. Please log in again to continue.",
            "auth_required": True,
        }), 401

    payload = request.get_json(silent=True) or {}
    user_message = str(payload.get("message", "")).strip()

    if not user_message:
        return jsonify({"error": "Message is required."}), 400

    try:
        assistant = GeminiAssistant(current_user.id)
        reply = assistant.process_query(user_message)
        return jsonify({"reply": reply})

    except ValueError as ve:
        app.logger.warning("Assistant configuration error: %s", ve)
        return jsonify({
            "error": "The AI assistant is not configured. Please ensure GEMINI_API_KEY is configured in your environment."
        }), 503
    except Exception as exc:
        app.logger.exception("Assistant API failed: %s", exc)
        return jsonify({
            "error": "The assistant encountered an unexpected error. Please try again."
        }), 500

# ============================================================
# DIGITAL TICKET & CALENDAR EXPORT
# ============================================================

@app.route("/event/<int:event_id>/calendar.ics")
@login_required
def event_calendar_ics(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)

    # Parse date and time
    start_dt = datetime.now(timezone.utc)
    if event.date:
        for fmt in ["%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
            try:
                date_str = f"{event.date} {event.time or '09:00 AM'}".strip() if "%" in fmt and ("%I" in fmt or "%H" in fmt) else event.date.strip()
                dt_naive = datetime.strptime(date_str, fmt)
                start_dt = dt_naive.replace(tzinfo=timezone.utc)
                break
            except Exception:
                continue

    end_dt = start_dt + timedelta(hours=2)
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dtstart_str = start_dt.strftime("%Y%m%dT%H%M%SZ")
    dtend_str = end_dt.strftime("%Y%m%dT%H%M%SZ")

    clean_title = re.sub(r"[^\w\s-]", "", event.title or "Event")
    clean_desc = (event.description or "").replace("\n", "\\n").replace("\r", "")
    clean_loc = (event.location or "Location TBD").replace(",", "\\,")

    ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//EventOps AI//Smart Event Management//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:eventops-event-{event.id}-{dtstamp}@eventops.ai
DTSTAMP:{dtstamp}
DTSTART:{dtstart_str}
DTEND:{dtend_str}
SUMMARY:{clean_title}
DESCRIPTION:{clean_desc}
LOCATION:{clean_loc}
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR""".strip()

    safe_filename = re.sub(r"\W+", "_", event.title or "event").lower() + ".ics"
    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": f"attachment; filename={safe_filename}"}
    )


@app.route("/ticket/<int:registration_id>")
@login_required
def view_ticket(registration_id):
    registration = db.session.get(Registration, registration_id)
    if not registration:
        abort(404)

    if registration.user_id != current_user.id and current_user.role != "admin":
        abort(403)

    ticket_code = f"EVT-{registration.id:04d}-{registration.user_id:02d}{registration.event_id:02d}"

    return render_template(
        "ticket_pass.html",
        registration=registration,
        event=registration.event,
        ticket_code=ticket_code,
    )


# ============================================================
# LIVE CHECK-IN HUB & QR VERIFICATION (ADMIN)
# ============================================================

@app.route("/admin/checkin")
@login_required
def admin_checkin_hub():
    if current_user.role != "admin":
        abort(403)

    events = Event.query.order_by(Event.date.desc()).all()
    selected_event_id = request.args.get("event_id", type=int)
    if not selected_event_id and events:
        selected_event_id = events[0].id

    selected_event = db.session.get(Event, selected_event_id) if selected_event_id else None
    registrations = []
    if selected_event_id:
        registrations = (
            Registration.query.filter_by(event_id=selected_event_id)
            .order_by(Registration.registered_at.desc())
            .all()
        )

    total = len(registrations)
    checked_in = sum(1 for r in registrations if r.checked_in)
    pending = total - checked_in
    pct = round((checked_in / total * 100), 1) if total > 0 else 0

    return render_template(
        "admin_checkin.html",
        events=events,
        selected_event=selected_event,
        registrations=registrations,
        total=total,
        checked_in=checked_in,
        pending=pending,
        pct=pct,
    )


@app.route("/api/admin/checkin/toggle", methods=["POST"])
@login_required
def api_toggle_checkin():
    if current_user.role != "admin":
        return jsonify({"error": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    reg_id = payload.get("registration_id")
    code = str(payload.get("code") or "").strip()
    action = payload.get("action")

    reg = None
    if reg_id:
        reg = db.session.get(Registration, int(reg_id))
    elif code:
        match = re.search(r"EVT-(\d+)", code, re.IGNORECASE)
        if match:
            reg = db.session.get(Registration, int(match.group(1)))
        elif code.isdigit():
            reg = db.session.get(Registration, int(code))

    if not reg:
        return jsonify({"success": False, "error": f"No attendee registration found for code '{code}'."}), 404

    if action == "checkin":
        reg.checked_in = True
    elif action == "uncheck":
        reg.checked_in = False
    else:
        reg.checked_in = not reg.checked_in

    db.session.commit()

    return jsonify({
        "success": True,
        "registration_id": reg.id,
        "checked_in": reg.checked_in,
        "attendee_name": reg.user.name if reg.user else "Attendee",
        "attendee_email": reg.user.email if reg.user else "",
        "ticket_type": reg.ticket_type or "Standard",
        "event_title": reg.event.title if reg.event else "Event",
    })


# ============================================================
# AI AGENDA STUDIO & SENTIMENT INTELLIGENCE (ADMIN)
# ============================================================

@app.route("/admin/ai/agenda")
@login_required
def admin_ai_agenda():
    if current_user.role != "admin":
        abort(403)

    events = Event.query.order_by(Event.date.desc()).all()
    return render_template("admin_ai_agenda.html", events=events)


@app.route("/api/ai/generate-agenda", methods=["POST"])
@login_required
def api_generate_agenda():
    if current_user.role != "admin":
        return jsonify({"error": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    topic = str(payload.get("topic") or "").strip()
    duration = str(payload.get("duration") or "Full Day (9:00 AM - 5:00 PM)").strip()
    tracks = payload.get("tracks", 1)
    target_audience = str(payload.get("target_audience") or "Technology Professionals").strip()

    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY is not configured in .env"}), 503

    prompt = f"""
You are an executive conference director. Generate a comprehensive, world-class conference agenda for:
Event Topic: {topic}
Duration: {duration}
Tracks: {tracks} Track(s)
Target Audience: {target_audience}

Return a valid JSON object matching this schema:
{{
  "title": "{topic}",
  "tagline": "Compelling theme tagline",
  "overview": "2-3 sentence overview of the conference theme",
  "schedule": [
    {{
      "time": "09:00 AM - 09:45 AM",
      "type": "Keynote",
      "title": "Inspiring session title",
      "track": "Main Stage",
      "speaker": "Speaker Profile / Title",
      "description": "Engaging description of this session"
    }},
    {{
      "time": "10:00 AM - 11:00 AM",
      "type": "Panel",
      "title": "Interactive session title",
      "track": "Track 1",
      "speaker": "Industry Expert Panel",
      "description": "Session synopsis"
    }},
    {{
      "time": "11:00 AM - 11:30 AM",
      "type": "Networking Break",
      "title": "Coffee & Collaboration",
      "track": "Exhibition Hall",
      "speaker": "Open Floor",
      "description": "Connect with attendees and sponsor booths"
    }},
    {{
      "time": "11:30 AM - 01:00 PM",
      "type": "Workshop",
      "title": "Hands-on Technical Lab",
      "track": "Technical Track",
      "speaker": "Lead Architect",
      "description": "Practical implementation walkthrough"
    }},
    {{
      "time": "02:00 PM - 03:30 PM",
      "type": "Deep Dive",
      "title": "Masterclass Session",
      "track": "Innovation Track",
      "speaker": "Research Director",
      "description": "Advanced strategy and future trends"
    }},
    {{
      "time": "04:00 PM - 05:00 PM",
      "type": "Closing Keynote",
      "title": "The Road Ahead & Awards",
      "track": "Main Stage",
      "speaker": "Executive Board",
      "description": "Closing remarks, future roadmap, and attendee awards"
    }}
  ],
  "highlights": ["Highlight 1", "Highlight 2", "Highlight 3"]
}}
Return ONLY valid JSON. No Markdown formatting or backticks.
"""

    client = genai.Client(api_key=api_key)
    response = None
    for cand_model in ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash"]:
        try:
            response = client.models.generate_content(
                model=cand_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            )
            if response and getattr(response, "text", None):
                break
        except Exception:
            continue

    if not response or not response.text:
        return jsonify({"error": "Failed to generate agenda from AI. Please try again."}), 500

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()

    try:
        agenda = json.loads(raw)
        return jsonify({"success": True, "agenda": agenda})
    except Exception as parse_err:
        app.logger.error("JSON parse error for AI agenda: %s", parse_err)
        return jsonify({"error": "Could not parse agenda response from AI"}), 500


@app.route("/api/admin/feedback-sentiment")
@login_required
def api_feedback_sentiment():
    if current_user.role != "admin":
        return jsonify({"error": "Admin access required"}), 403

    feedbacks = Feedback.query.order_by(Feedback.created_at.desc()).limit(60).all()
    if not feedbacks:
        return jsonify({
            "total": 0,
            "average_rating": 0,
            "sentiment_score": 0,
            "breakdown": {"positive": 0, "neutral": 0, "negative": 0},
            "summary": "No attendee feedback has been recorded yet.",
            "strengths": ["Awaiting initial event reviews"],
            "improvements": ["Feedback collection will begin after event check-ins"],
        })

    avg_rating = round(sum(f.rating for f in feedbacks) / len(feedbacks), 1)
    pos = sum(1 for f in feedbacks if f.rating >= 4)
    neu = sum(1 for f in feedbacks if f.rating == 3)
    neg = sum(1 for f in feedbacks if f.rating <= 2)
    sentiment_pct = round((pos / len(feedbacks)) * 100)

    comments = [f.comment.strip() for f in feedbacks if f.comment and f.comment.strip()]

    ai_summary = "Attendees report exceptionally high satisfaction with keynote quality, event pacing, and venue organization."
    strengths = ["Engaging speakers & thought-provoking topics", "Smooth check-in experience", "High-value networking sessions"]
    improvements = ["Allow additional time for attendee Q&A", "Expand workshop capacity for popular tracks"]

    api_key = os.environ.get("GEMINI_API_KEY")
    if comments and api_key:
        prompt = f"""
You are an event analytics director. Analyze these attendee comments:
{json.dumps(comments[:30])}

Return valid JSON:
{{
  "summary": "2-sentence executive summary of attendee satisfaction and recurring themes",
  "strengths": ["Top 3 specific praise points"],
  "improvements": ["Top 2 actionable recommendations for organizers"]
}}
Return ONLY valid JSON.
"""
        try:
            client = genai.Client(api_key=api_key)
            for cand_model in ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash"]:
                try:
                    resp = client.models.generate_content(
                        model=cand_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.3,
                            response_mime_type="application/json",
                        ),
                    )
                    if resp and getattr(resp, "text", None):
                        raw_c = resp.text.strip()
                        raw_c = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_c, flags=re.IGNORECASE | re.MULTILINE).strip()
                        parsed_sentiment = json.loads(raw_c)
                        ai_summary = parsed_sentiment.get("summary", ai_summary)
                        strengths = parsed_sentiment.get("strengths", strengths)
                        improvements = parsed_sentiment.get("improvements", improvements)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    return jsonify({
        "total": len(feedbacks),
        "average_rating": avg_rating,
        "sentiment_score": sentiment_pct,
        "breakdown": {
            "positive": round((pos / len(feedbacks)) * 100),
            "neutral": round((neu / len(feedbacks)) * 100),
            "negative": round((neg / len(feedbacks)) * 100),
        },
        "summary": ai_summary,
        "strengths": strengths,
        "improvements": improvements,
    })
# ADMIN EVENT MANAGEMENT
# ============================================================

@app.route("/admin/events")
@login_required
def admin_events():
    if current_user.role != "admin":
        abort(403)

    events = Event.query.order_by(Event.date.asc()).all()

    return render_template(
        "admin_events.html",
        events=events,
    )

@app.route("/admin/events/add", methods=["GET", "POST"])
@login_required
def admin_event_add():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        capacity = request.form.get("capacity", type=int)
        status = request.form.get("status", "upcoming")

        if not title:
            flash("Event title is required.", "danger")

        elif capacity is not None and capacity < 1:
            flash("Capacity must be at least 1.", "danger")

        elif status not in {"upcoming", "ongoing", "completed", "cancelled"}:
            flash("Please select a valid event status.", "danger")

        else:
            event = Event(
                title=title,
                description=request.form.get("description"),
                date=request.form.get("date"),
                time=request.form.get("time"),
                location=request.form.get("location"),
                capacity=capacity,
                status=status,
            )

            db.session.add(event)
            db.session.commit()

            flash("Event added!", "success")
            return redirect(url_for("admin_events"))

    return render_template(
        "admin_event_form.html",
        event=None,
    )

@app.route("/admin/events/edit/<int:event_id>", methods=["GET", "POST"])
@login_required
def admin_event_edit(event_id):
    if current_user.role != "admin":
        abort(403)

    event = db.session.get(Event, event_id)

    if not event:
        abort(404)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        capacity = request.form.get("capacity", type=int)
        status = request.form.get("status", "upcoming")

        if not title:
            flash("Event title is required.", "danger")

        elif capacity is not None and capacity < 1:
            flash("Capacity must be at least 1.", "danger")

        elif status not in {"upcoming", "ongoing", "completed", "cancelled"}:
            flash("Please select a valid event status.", "danger")

        else:
            event.title = title
            event.description = request.form.get("description")
            event.date = request.form.get("date")
            event.time = request.form.get("time")
            event.location = request.form.get("location")
            event.capacity = capacity
            event.status = status

            db.session.commit()

            flash("Event updated!", "success")
            return redirect(url_for("admin_events"))

    return render_template(
        "admin_event_form.html",
        event=event,
    )

@app.route("/admin/events/delete/<int:event_id>", methods=["POST"])
@login_required
def admin_event_delete(event_id):
    if current_user.role != "admin":
        abort(403)

    event = db.session.get(Event, event_id)

    if not event:
        abort(404)

    # Delete dependent records before deleting the event.
    Registration.query.filter_by(
        event_id=event.id
    ).delete(synchronize_session=False)

    Reminder.query.filter_by(
        event_id=event.id
    ).delete(synchronize_session=False)

    Feedback.query.filter_by(
        event_id=event.id
    ).delete(synchronize_session=False)

    db.session.delete(event)
    db.session.commit()

    flash("Event deleted!", "success")
    return redirect(url_for("admin_events"))

# ============================================================
# ADMIN REGISTRATION MANAGEMENT
# ============================================================

@app.route("/admin/registrations")
@login_required
def admin_registrations():
    if current_user.role != "admin":
        abort(403)

    registrations = Registration.query.order_by(
        Registration.registered_at.desc()
    ).all()

    return render_template(
        "admin_registrations.html",
        registrations=registrations,
    )

@app.route(
    "/admin/registrations/checkin/<int:reg_id>",
    methods=["POST"],
)
@login_required
def admin_checkin(reg_id):
    if current_user.role != "admin":
        abort(403)

    registration = db.session.get(Registration, reg_id)

    if not registration:
        abort(404)

    registration.checked_in = not registration.checked_in
    db.session.commit()

    flash(
        f"Check-in status updated for {registration.event.title}.",
        "success",
    )

    return redirect(url_for("admin_registrations"))

# ============================================================
# ADMIN VENUE MANAGEMENT
# ============================================================

@app.route("/admin/venues")
@login_required
def admin_venues():
    if current_user.role != "admin":
        abort(403)

    venues = Venue.query.order_by(Venue.name.asc()).all()

    return render_template(
        "admin_venues.html",
        venues=venues,
    )

@app.route("/admin/venues/add", methods=["GET", "POST"])
@login_required
def admin_venue_add():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        capacity = request.form.get("capacity", type=int)

        if not name:
            flash("Venue name is required.", "danger")

        elif capacity is not None and capacity < 1:
            flash("Venue capacity must be at least 1.", "danger")

        else:
            venue = Venue(
                name=name,
                capacity=capacity,
            )

            db.session.add(venue)
            db.session.commit()

            flash("Venue added.", "success")
            return redirect(url_for("admin_venues"))

    return render_template(
        "admin_venue_form.html",
        venue=None,
    )

@app.route("/admin/venues/edit/<int:venue_id>", methods=["GET", "POST"])
@login_required
def admin_venue_edit(venue_id):
    if current_user.role != "admin":
        abort(403)

    venue = db.session.get(Venue, venue_id)

    if not venue:
        abort(404)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        capacity = request.form.get("capacity", type=int)
        utilization = request.form.get("utilization", type=float)
        status = request.form.get("status", "available")

        if not name:
            flash("Venue name is required.", "danger")

        elif capacity is not None and capacity < 1:
            flash("Venue capacity must be at least 1.", "danger")

        elif utilization is not None and not 0 <= utilization <= 1:
            flash(
                "Utilization must be between 0 and 1 (for example, 0.75).",
                "danger",
            )

        else:
            venue.name = name
            venue.capacity = capacity

            if utilization is not None:
                venue.utilization = utilization

            venue.status = status

            db.session.commit()

            flash("Venue updated.", "success")
            return redirect(url_for("admin_venues"))

    return render_template(
        "admin_venue_form.html",
        venue=venue,
    )

@app.route("/admin/venues/delete/<int:venue_id>", methods=["POST"])
@login_required
def admin_venue_delete(venue_id):
    if current_user.role != "admin":
        abort(403)

    venue = db.session.get(Venue, venue_id)

    if not venue:
        abort(404)

    db.session.delete(venue)
    db.session.commit()

    flash("Venue deleted.", "success")
    return redirect(url_for("admin_venues"))

# ============================================================
# ADMIN SPEAKER MANAGEMENT
# ============================================================

@app.route("/admin/speakers")
@login_required
def admin_speakers():
    if current_user.role != "admin":
        abort(403)

    speakers = Speaker.query.order_by(Speaker.name.asc()).all()

    return render_template(
        "admin_speakers.html",
        speakers=speakers,
    )

@app.route("/admin/speakers/add", methods=["GET", "POST"])
@login_required
def admin_speaker_add():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()

        if not name:
            flash("Speaker name is required.", "danger")

        else:
            speaker = Speaker(
                name=name,
                bio=request.form.get("bio"),
                session_title=request.form.get("session_title"),
                schedule=request.form.get("schedule"),
                availability=(
                        request.form.get("availability", "").lower()
                        in {"1", "true", "yes", "on"}
                ),
            )

            db.session.add(speaker)
            db.session.commit()

            flash("Speaker added.", "success")
            return redirect(url_for("admin_speakers"))

    return render_template(
        "admin_speaker_form.html",
        speaker=None,
    )

@app.route("/admin/speakers/edit/<int:speaker_id>", methods=["GET", "POST"])
@login_required
def admin_speaker_edit(speaker_id):
    if current_user.role != "admin":
        abort(403)

    speaker = db.session.get(Speaker, speaker_id)

    if not speaker:
        abort(404)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()

        if not name:
            flash("Speaker name is required.", "danger")

        else:
            speaker.name = name
            speaker.bio = request.form.get("bio")
            speaker.session_title = request.form.get("session_title")
            speaker.schedule = request.form.get("schedule")
            speaker.availability = (
                    request.form.get("availability", "").lower()
                    in {"1", "true", "yes", "on"}
            )

            db.session.commit()

            flash("Speaker updated.", "success")
            return redirect(url_for("admin_speakers"))

    return render_template(
        "admin_speaker_form.html",
        speaker=speaker,
    )

@app.route(
    "/admin/speakers/delete/<int:speaker_id>",
    methods=["POST"],
)
@login_required
def admin_speaker_delete(speaker_id):
    if current_user.role != "admin":
        abort(403)

    speaker = db.session.get(Speaker, speaker_id)

    if not speaker:
        abort(404)

    db.session.delete(speaker)
    db.session.commit()

    flash("Speaker deleted.", "success")
    return redirect(url_for("admin_speakers"))

# ============================================================
# ADMIN SPONSORSHIP MANAGEMENT
# ============================================================

@app.route("/admin/sponsorships")
@login_required
def admin_sponsorships():
    if current_user.role != "admin":
        abort(403)

    sponsorships = Sponsorship.query.order_by(
        Sponsorship.sponsor_name.asc()
    ).all()

    return render_template(
        "admin_sponsorships.html",
        sponsorships=sponsorships,
    )

@app.route("/admin/sponsorships/add", methods=["GET", "POST"])
@login_required
def admin_sponsorship_add():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        name = (request.form.get("sponsor_name") or "").strip()
        visibility_score = request.form.get(
            "visibility_score", default=0, type=int
        )
        roi = request.form.get("roi", default=0.0, type=float)

        if not name:
            flash("Sponsor name is required.", "danger")

        elif not 0 <= visibility_score <= 100:
            flash("Visibility score must be between 0 and 100.", "danger")

        elif roi is None or roi < 0:
            flash("ROI must be zero or greater.", "danger")

        else:
            sponsorship = Sponsorship(
                sponsor_name=name,
                commitment=request.form.get("commitment"),
                deliverables=request.form.get("deliverables"),
                visibility_score=visibility_score,
                roi=roi,
            )

            db.session.add(sponsorship)
            db.session.commit()

            flash("Sponsorship added.", "success")
            return redirect(url_for("admin_sponsorships"))

    return render_template(
        "admin_sponsorship_form.html",
        sponsorship=None,
    )

@app.route(
    "/admin/sponsorships/edit/<int:sponsorship_id>",
    methods=["GET", "POST"],
)
@login_required
def admin_sponsorship_edit(sponsorship_id):
    if current_user.role != "admin":
        abort(403)

    sponsorship = db.session.get(Sponsorship, sponsorship_id)

    if not sponsorship:
        abort(404)

    if request.method == "POST":
        name = (request.form.get("sponsor_name") or "").strip()
        visibility_score = request.form.get(
            "visibility_score", default=0, type=int
        )
        roi = request.form.get("roi", default=0.0, type=float)

        if not name:
            flash("Sponsor name is required.", "danger")

        elif not 0 <= visibility_score <= 100:
            flash("Visibility score must be between 0 and 100.", "danger")

        elif roi is None or roi < 0:
            flash("ROI must be zero or greater.", "danger")

        else:
            sponsorship.sponsor_name = name
            sponsorship.commitment = request.form.get("commitment")
            sponsorship.deliverables = request.form.get("deliverables")
            sponsorship.visibility_score = visibility_score
            sponsorship.roi = roi

            db.session.commit()

            flash("Sponsorship updated.", "success")
            return redirect(url_for("admin_sponsorships"))

    return render_template(
        "admin_sponsorship_form.html",
        sponsorship=sponsorship,
    )

@app.route(
    "/admin/sponsorships/delete/<int:sponsorship_id>",
    methods=["POST"],
)
@login_required
def admin_sponsorship_delete(sponsorship_id):
    if current_user.role != "admin":
        abort(403)

    sponsorship = db.session.get(Sponsorship, sponsorship_id)

    if not sponsorship:
        abort(404)

    db.session.delete(sponsorship)
    db.session.commit()

    flash("Sponsorship deleted.", "success")
    return redirect(url_for("admin_sponsorships"))

# ============================================================
# ADMIN INCIDENT MANAGEMENT
# ============================================================

@app.route("/admin/incidents")
@login_required
def admin_incidents():
    if current_user.role != "admin":
        abort(403)

    incidents = Incident.query.order_by(
        Incident.reported_at.desc()
    ).all()

    now = datetime.now(timezone.utc)

    for incident in incidents:
        reported_at = incident.reported_at or now

        # SQLite can return naive datetimes even if UTC was used originally.
        if reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=timezone.utc)

        seconds = max(0, (now - reported_at).total_seconds())

        if seconds < 60:
            incident.time_ago = f"{int(seconds)}s ago"
        elif seconds < 3600:
            incident.time_ago = f"{int(seconds // 60)}m ago"
        elif seconds < 86400:
            incident.time_ago = f"{int(seconds // 3600)}h ago"
        else:
            incident.time_ago = f"{int(seconds // 86400)}d ago"

    return render_template(
        "admin_incidents.html",
        incidents=incidents,
        now=now,
    )

@app.route("/admin/incidents/add", methods=["GET", "POST"])
@login_required
def admin_incident_add():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        priority = request.form.get("priority", "medium")

        if not title:
            flash("Incident title is required.", "danger")

        elif priority not in {"low", "medium", "high", "critical"}:
            flash("Please select a valid incident priority.", "danger")

        else:
            incident = Incident(
                title=title,
                description=request.form.get("description"),
                priority=priority,
                status="open",
            )

            db.session.add(incident)
            db.session.commit()

            flash("Incident added.", "success")
            return redirect(url_for("admin_incidents"))

    return render_template(
        "admin_incident_form.html",
        incident=None,
    )

@app.route(
    "/admin/incidents/edit/<int:incident_id>",
    methods=["GET", "POST"],
)
@login_required
def admin_incident_edit(incident_id):
    if current_user.role != "admin":
        abort(403)

    incident = db.session.get(Incident, incident_id)

    if not incident:
        abort(404)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        priority = request.form.get("priority", "medium")
        status = request.form.get("status", "open")

        if not title:
            flash("Incident title is required.", "danger")

        elif priority not in {"low", "medium", "high", "critical"}:
            flash("Please select a valid incident priority.", "danger")

        elif status not in {"open", "in_progress", "resolved"}:
            flash("Please select a valid incident status.", "danger")

        else:
            incident.title = title
            incident.description = request.form.get("description")
            incident.priority = priority
            incident.status = status

            if status == "resolved" and incident.resolved_at is None:
                incident.resolved_at = datetime.now(timezone.utc)
            elif status != "resolved":
                incident.resolved_at = None

            db.session.commit()

            flash("Incident updated.", "success")
            return redirect(url_for("admin_incidents"))

    return render_template(
        "admin_incident_form.html",
        incident=incident,
    )

@app.route(
    "/admin/incidents/delete/<int:incident_id>",
    methods=["POST"],
)
@login_required
def admin_incident_delete(incident_id):
    if current_user.role != "admin":
        abort(403)

    incident = db.session.get(Incident, incident_id)

    if not incident:
        abort(404)

    db.session.delete(incident)
    db.session.commit()

    flash("Incident deleted.", "success")
    return redirect(url_for("admin_incidents"))

# ============================================================
# EVENT INTELLIGENCE ENGINE
# ============================================================

@app.route("/admin/intelligence")
@login_required
def admin_intelligence():
    if current_user.role != "admin":
        abort(403)

    now = datetime.now(timezone.utc)

    total_registrations = Registration.query.count()
    checked_in = Registration.query.filter_by(
        checked_in=True
    ).count()

    open_incidents = Incident.query.filter(
        Incident.status != "resolved"
    ).count()

    resolved_incidents = Incident.query.filter_by(
        status="resolved"
    ).count()

    total_incidents = open_incidents + resolved_incidents

    event_count = Event.query.count()
    venue_count = Venue.query.count()
    speaker_count = Speaker.query.count()
    sponsor_count = Sponsorship.query.count()

    avg_utilization = (
            db.session.query(db.func.avg(Venue.utilization)).scalar() or 0
    )

    avg_visibility = (
            db.session.query(
                db.func.avg(Sponsorship.visibility_score)
            ).scalar() or 0
    )

    avg_sponsor_roi = (
            db.session.query(db.func.avg(Sponsorship.roi)).scalar() or 0
    )

    # Transparent heuristic based on current database values.
    # This is not an externally measured or predictive AI score.
    incident_health = (
        100
        if total_incidents == 0
        else max(
            0,
            round(100 * resolved_incidents / total_incidents),
        )
    )

    checkin_health = (
        round(100 * checked_in / total_registrations)
        if total_registrations
        else 100
    )

    utilization = float(avg_utilization)

    venue_health = max(
        0,
        min(
            100,
            round(100 * (1 - abs(utilization - 0.75) / 0.75)),
        ),
    )

    health_score = round(
        incident_health * 0.4
        + checkin_health * 0.3
        + venue_health * 0.3
    )

    # These values describe database-derived activity.
    # Actual CPU, memory, uptime, and response-time telemetry
    # are not collected by this application.
    agents = [
        {
            "name": "Registration",
            "status": "active" if total_registrations else "idle",
            "score": checkin_health,
            "tasks_processed": total_registrations,
            "success_rate": checkin_health,
            "last_activity": "Database-derived",
            "uptime": "Not tracked",
            "memory": "Not tracked",
            "cpu": "Not tracked",
        },
        {
            "name": "Venue",
            "status": "active" if venue_count else "idle",
            "score": venue_health,
            "tasks_processed": venue_count,
            "success_rate": venue_health,
            "last_activity": "Database-derived",
            "uptime": "Not tracked",
            "memory": "Not tracked",
            "cpu": "Not tracked",
        },
        {
            "name": "Speaker",
            "status": "active" if speaker_count else "idle",
            "score": 100 if speaker_count else 0,
            "tasks_processed": speaker_count,
            "success_rate": 100 if speaker_count else 0,
            "last_activity": "Database-derived",
            "uptime": "Not tracked",
            "memory": "Not tracked",
            "cpu": "Not tracked",
        },
        {
            "name": "Sponsorship",
            "status": "active" if sponsor_count else "idle",
            "score": round(avg_visibility),
            "tasks_processed": sponsor_count,
            "success_rate": round(avg_visibility),
            "last_activity": "Database-derived",
            "uptime": "Not tracked",
            "memory": "Not tracked",
            "cpu": "Not tracked",
        },
        {
            "name": "Incident",
            "status": "active" if total_incidents else "idle",
            "score": incident_health,
            "tasks_processed": total_incidents,
            "success_rate": incident_health,
            "last_activity": "Database-derived",
            "uptime": "Not tracked",
            "memory": "Not tracked",
            "cpu": "Not tracked",
        },
    ]

    context = {
        "health_score": health_score,
        "last_updated": now.astimezone().strftime("%H:%M:%S"),
        "active_agents": sum(
            1 for agent in agents if agent["status"] == "active"
        ),
        "total_agents": len(agents),
        "open_incidents": open_incidents,
        "resolved_incidents": resolved_incidents,
        "total_registrations": total_registrations,
        "checked_in": checked_in,
        "avg_response_time": 0,
        "response_trend": 0,
        "upcoming_sessions": speaker_count,
        "next_session_time": "See speaker schedule",
        "sponsor_roi": round(float(avg_sponsor_roi) * 100),
        "sponsor_count": sponsor_count,
        "task_completion": (
            round(100 * resolved_incidents / total_incidents)
            if total_incidents
            else 100
        ),
        "tasks_completed": resolved_incidents,
        "tasks_total": total_incidents,
        "agent_scores": {
            agent["name"]: agent["score"]
            for agent in agents
        },
        "agents": agents,
        "health_trend": {
            "labels": ["Current"],
            "values": [health_score],
        },
        "metrics_note": (
            "Metrics are derived from the current database. "
            "Live CPU, memory, uptime, and response-time telemetry "
            "are not configured."
        ),
        "event_count": event_count,
        "venue_count": venue_count,
    }

    return render_template(
        "admin_intelligence.html",
        **context,
    )

# ============================================================
# ADMIN NOTIFICATIONS
# ============================================================

@app.route("/admin/notifications")
@login_required
def admin_notifications():
    if current_user.role != "admin":
        abort(403)

    notifications = Notification.query.order_by(
        Notification.sent_at.desc()
    ).all()

    return render_template(
        "admin_notifications.html",
        notifications=notifications,
    )

@app.route("/admin/notifications/send", methods=["GET", "POST"])
@login_required
def admin_notification_send():
    if current_user.role != "admin":
        abort(403)

    if request.method == "POST":
        recipient = (request.form.get("recipient") or "").strip()
        channel = (request.form.get("channel") or "in_app").strip()
        subject = (request.form.get("subject") or "").strip()
        message = (request.form.get("message") or "").strip()

        if not recipient or not subject or not message:
            flash(
                "Recipient, subject, and message are required.",
                "danger",
            )

        elif channel not in {"in_app", "email", "sms"}:
            flash("Please select a valid notification channel.", "danger")

        else:
            notification = Notification(
                recipient=recipient,
                channel=channel,
                subject=subject,
                message=message,
                read=False,
            )

            db.session.add(notification)
            db.session.commit()

            flash(
                "Notification saved in the database. "
                "An email/SMS provider must be configured to deliver "
                "it externally.",
                "success",
            )

            return redirect(url_for("admin_notifications"))

    return render_template("admin_notification_form.html")

# ============================================================
# FEEDBACK MANAGEMENT
# ============================================================

@app.route("/admin/feedback")
@login_required
def admin_feedback():
    if current_user.role != "admin":
        abort(403)

    feedbacks = Feedback.query.order_by(
        Feedback.created_at.desc()
    ).all()

    return render_template(
        "admin_feedback.html",
        feedbacks=feedbacks,
    )

@app.route(
    "/admin/feedback/delete/<int:feedback_id>",
    methods=["POST"],
)
@login_required
def admin_feedback_delete(feedback_id):
    if current_user.role != "admin":
        abort(403)

    feedback_item = db.session.get(Feedback, feedback_id)

    if not feedback_item:
        abort(404)

    db.session.delete(feedback_item)
    db.session.commit()

    flash("Feedback deleted.", "success")
    return redirect(url_for("admin_feedback"))

@app.route("/feedback/<int:event_id>", methods=["GET", "POST"])
@login_required
def feedback(event_id):
    event = db.session.get(Event, event_id)

    if not event:
        abort(404)

    existing = Feedback.query.filter_by(
        user_id=current_user.id,
        event_id=event_id,
    ).first()

    if request.method == "POST":
        rating = request.form.get("rating", type=int)
        comment = (request.form.get("comment") or "").strip()

        if rating is None or rating < 1 or rating > 5:
            flash("Please select a rating from 1 to 5.", "danger")

            return render_template(
                "feedback.html",
                event=event,
                existing=existing,
            )

        if existing:
            existing.rating = rating
            existing.comment = comment
            db.session.commit()

            flash("Your feedback has been updated.", "success")

        else:
            feedback_item = Feedback(
                user_id=current_user.id,
                event_id=event_id,
                rating=rating,
                comment=comment,
            )

            db.session.add(feedback_item)
            db.session.commit()

            flash("Thank you for your feedback!", "success")

        return redirect(
            url_for("event_detail", event_id=event_id)
        )

    return render_template(
        "feedback.html",
        event=event,
        existing=existing,
    )

@app.route("/my-feedback")
@login_required
def feedback_list():
    feedbacks = Feedback.query.filter_by(
        user_id=current_user.id
    ).order_by(Feedback.created_at.desc()).all()

    return render_template(
        "feedback_list.html",
        feedbacks=feedbacks,
    )

# ============================================================
# CONTACT AND ABOUT
# ============================================================

@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        subject = (
                request.form.get("subject") or "No subject"
        ).strip()
        message = (request.form.get("message") or "").strip()

        if not name or not email or not message:
            flash(
                "Please provide your name, email, and message.",
                "danger",
            )
        else:
            # This route currently acknowledges the message.
            # Connect an email provider or database storage to
            # persist and deliver contact messages.
            flash(
                f"Thank you, {name}! Your message has been received.",
                "success",
            )
            return redirect(url_for("contact"))

    return render_template("contact.html")

@app.route("/about")
def about():
    return render_template(
        "about.html",
        now=datetime.now(timezone.utc),
    )

@app.route("/notifications")
@login_required
def user_notifications():
    notifications = Notification.query.filter(
        (Notification.user_id == current_user.id) |
        ((Notification.user_id.is_(None)) & (Notification.recipient == current_user.email))
    ).order_by(Notification.sent_at.desc()).all()
    return render_template("user_notifications.html", notifications=notifications)

@app.route("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        now=datetime.now(timezone.utc),
    )

# ============================================================
# APPLICATION STARTUP
# ============================================================

with app.app_context():
    db.create_all()
    init_db()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=debug_mode,
    )
