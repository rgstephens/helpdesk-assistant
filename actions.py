import logging
from typing import Dict, Text, Any, List, Union, Optional
from rasa_sdk import Tracker, Action, DialogueStateTracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormAction
from rasa_sdk.events import (
    AllSlotsReset,
    SlotSet,
    SessionStarted,
    ActionExecuted,
    EventType,
    Restarted,
)
import ruamel.yaml

import requests
import json

logger = logging.getLogger(__name__)
vers = "vers: 0.2.1, date: Apr 4, 2020"
logger.debug(vers)

snow_config = ruamel.yaml.safe_load(open("snow_credentials.yml", "r")) or {}
snow_user = snow_config.get("snow_user")
snow_pw = snow_config.get("snow_pw")
snow_instance = snow_config.get("snow_instance")
localmode = snow_config.get("localmode", True)
use_profile_email = snow_config.get("use_profile_email", True)
logger.debug(f"Local mode: {localmode}")

base_api_url = "https://{}/api/now".format(snow_instance)


def generate_mock_profile():
    import random

    user = [
        {"name": "Abel", "email": "abel.tuter@example.com"},
        {"name": "Abraham", "email": "abraham.lincoln@example.com"},
        {"name": "Adela", "email": "adela.cervantsz@example.com"},
        {"name": "Aileen", "email": "aileen.mottern@example.com"},
        {"name": "Allyson", "email": "allyson.gillispie@example.com"},
        {"name": "Alva", "email": "alva.pennigton@example.com"},
        {"name": "Amos", "email": "amos.linnan@example.com"},
    ]
    sites = [
        "Berlin",
        "San Francisco",
        "Seattle",
        "London",
        "Austin",
        "Dallas",
        "New York",
        "Zürich",
    ]
    n = random.randint(0, len(user) - 1)
    mock_profile = {
        "profile_name": user[n]["name"],
        "email": user[n]["email"] if use_profile_email else None,
        "profile_site": sites[random.randint(0, len(sites) - 1)],
    }
    return mock_profile


class ActionSessionStart(Action):
    def name(self) -> Text:
        return "action_session_start"

    @staticmethod
    def _slot_set_events_from_tracker(
        tracker: "DialogueStateTracker",
    ) -> List["SlotSet"]:
        """Fetch SlotSet events from tracker and
        carry over key, value and metadata."""

        return [
            SlotSet(key=event.key, value=event.value, metadata=event.metadata)
            for event in tracker.applied_events()
            if isinstance(event, SlotSet)
        ]

    async def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[EventType]:

        sender_id = tracker.sender_id
        logger.debug(
            f"action_session_start, run, sender_id: {sender_id}"
        )  # the session should begin with a `session_started` event
        events = [SessionStarted()]

        # any slots that should be carried over should come after the
        # `session_started` event`
        if "carry_over_slots" in domain.get("session_config", {}):
            events.extend(self._slot_set_events_from_tracker(tracker))

        # get mock user profile
        user_profile = generate_mock_profile()
        logger.debug(f"action_session_start, user_profile: {user_profile}")
        for key, value in user_profile.items():
            if value is not None:
                events.append(SlotSet(key=key, value=value))

        # an `action_listen` should be added at the end, user message follows
        events.append(ActionExecuted("action_listen"))

        return events


def email_to_sysid(email):
    lookup_url = f"{base_api_url}/table/sys_user?sysparm_limit=1&email={email}"
    user = snow_user
    pwd = snow_pw
    # Set proper headers
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }  # noqa: 501
    results = dict()
    results["status"] = 200
    # Do the HTTP request
    try:
        response = requests.get(lookup_url, auth=(user, pwd), headers=headers)
        if response.status_code == 200:
            results["value"] = response.json()["result"]
        else:
            results["status"] = response.status_code
            results["msg"] = (
                "ServiceNow error: " + response.json()["error"]["message"]
            )
    except requests.exceptions.Timeout:
        results["msg"] = "Could not connect to ServiceNow (Timeout)"
    return results


def create_incident(description, short_description, priority, caller):
    incident_url = "https://{}/api/now/table/incident".format(snow_instance)
    user = snow_user
    pwd = snow_pw
    # Set proper headers
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }  # noqa: 501
    data = {
        "opened_by": caller,
        "short_description": short_description,
        "description": description,
        "urgency": priority,
        "caller_id": caller,
        "comments": description,
    }
    response = requests.post(
        incident_url, auth=(user, pwd), headers=headers, data=json.dumps(data)
    )
    return response


class OpenIncidentForm(FormAction):
    def name(self) -> Text:
        return "open_incident_form"

    @staticmethod
    def required_slots(tracker: Tracker) -> List[Text]:
        """A list of required slots that the form has to fill"""

        return ["email", "priority", "problem_description", "incident_title"]

    def slot_mappings(self) -> Dict[Text, Union[Dict, List[Dict]]]:
        """A dictionary to map required slots to
            - an extracted entity
            - intent: value pairs
            - a whole message
            or a list of them, where a first match will be picked"""

        return {
            "email": self.from_entity(entity="email"),
            "priority": self.from_entity(entity="priority"),
            "problem_description": [
                self.from_text(
                    intent=["password_reset", "problem_email", "inform"]
                )
            ],
            "incident_title": [
                self.from_trigger_intent(
                    intent="password_reset", value="Problem resetting password"
                ),
                self.from_trigger_intent(
                    intent="problem_email", value="Problem with email"
                ),
                self.from_text(
                    intent=["password_reset", "problem_email", "inform"]
                ),
            ],
        }

    @staticmethod
    def priority_db() -> List[Text]:
        """Database of supported priorities"""

        return ["low", "medium", "high"]

    def validate_email(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate email is in ticket system."""
        if localmode:
            return {"email": value}
        results = email_to_sysid(value)

        if results["status"] == 200:
            # validation succeeded, set the value of the "email" slot to value
            if len(results["value"]) == 1:
                return {"email": value}
            else:
                dispatcher.utter_message(template="utter_no_email")
                return {"email": None}
        else:
            dispatcher.utter_message(results["msg"])
            # validation failed, set this slot to None, meaning the
            # user will be asked for the slot again
            return {"email": None}

    def validate_priority(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate priority is a valid value."""

        if value.lower() in self.priority_db():
            # validation succeeded,
            # set the value of the "priority" slot to value
            return {"priority": value}
        else:
            dispatcher.utter_message(template="utter_no_priority")
            # validation failed, set this slot to None, meaning the
            # user will be asked for the slot again
            return {"priority": None}

    def submit(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict]:
        """Define what the form has to do
            after all required slots are filled"""

        priority = tracker.get_slot("priority")
        email = tracker.get_slot("email")
        problem_description = tracker.get_slot("problem_description")
        incident_title = tracker.get_slot("incident_title")

        # Check priority and set number value accordingly
        if priority == "low":
            snow_priority = "3"
        elif priority == "medium":
            snow_priority = "2"
        else:
            snow_priority = "1"

        if localmode:
            message = (
                f"An incident with the following details would be opened \
                if ServiceNow was connected:\n"
                f"email: {email}\n"
                f"problem description: {problem_description}\n"
                f"title: {incident_title}\npriority: {priority}"
            )
        else:
            results = email_to_sysid(email)
            sysid = results["value"][0]["sys_id"]
            response = create_incident(
                description=problem_description,
                short_description=incident_title,
                priority=snow_priority,
                caller=sysid,
            )
            incident_number = response.json()["result"]["number"]
            message = (
                f"Successfully opened up incident {incident_number} for you.  "
                f"Someone will reach out soon."
            )
            # utter submit template
        dispatcher.utter_message(message)
        return [AllSlotsReset()]


class ActionVersion(Action):
    def name(self):
        return "action_version"

    def run(self, dispatcher, tracker, domain):
        try:
            request = json.loads(
                requests.get("http://rasa-x:5002/api/version").text
            )
            dispatcher.utter_message(
                f"Rasa X: {request['rasa-x']}\n"
                "Rasa:  {request['rasa']['production']}\nActions: {vers}"
            )
        except (ConnectionError, TimeoutError):
            dispatcher.utter_message(f"Can't connect to Rasa X")
        return []


class ActionRestart(Action):
    """Resets the tracker to its initial state.
    Utters the restart template if available."""

    def name(self):
        return "action_restart"

    def run(self, dispatcher, tracker, domain):
        return [Restarted()]


class ActionResetSlots(Action):
    """Resets the tracker to its initial state.
    Utters the restart template if available."""

    def name(self):
        return "action_reset_slots"

    def run(self, dispatcher, tracker, domain):
        events = [AllSlotsReset()]

        # generate mock profile
        user_profile = generate_mock_profile()
        logger.debug(f"action_reset_slots, user_profile: {user_profile}")
        for key, value in user_profile.items():
            if value is not None:
                events.append(SlotSet(key=key, value=value))

        return events


class ActionShowSlots(Action):
    def name(self):
        logger.info("ActionVersion self called")
        return "action_show_slots"

    def run(self, dispatcher, tracker, domain):
        msg = "Slots:\n"
        for k, v in tracker.slots.items():
            msg += f" {k} | {v}\n"
        dispatcher.utter_message(msg)
        return []


def get_last_event_for(
    tracker,
    event_type: Text,
    action_names_to_exclude: List[Text] = None,
    skip: int = 0,
) -> Optional[Any]:
    def filter_function(e):
        has_instance = e
        if e["event"] == event_type:
            has_instance = e
        excluded = e["event"] != event_type or (
            (
                e["event"] == event_type
                and (
                    (e["parse_data"]["intent"]["name"] == "domicile")
                    or (e["parse_data"]["intent"]["name"] == "customertype")
                )
            )
        )
        return has_instance and not excluded

    filtered = filter(filter_function, reversed(tracker.events))
    for i in range(skip):
        next(filtered, None)

    return next(filtered, None)


def intentHistoryStr(tracker, skip, past):
    msg = ""
    prev_user_event = get_last_event_for(tracker, "user", skip=skip)
    logger.info(
        "event.text: {}, intent: {}, confidence: {}".format(
            prev_user_event["text"],
            prev_user_event["parse_data"]["intent"]["name"],
            prev_user_event["parse_data"]["intent"]["confidence"],
        )
    )
    msg = "Ranked F1 scores:\n"
    msg += (
        "* "
        + prev_user_event["parse_data"]["intent"]["name"]
        + " ("
        + "{:.4f}".format(
            prev_user_event["parse_data"]["intent"]["confidence"]
        )
        + ")\n"
    )
    for i in range(past - 1):
        msg += (
            "* "
            + prev_user_event["parse_data"]["intent_ranking"][i + 1]["name"]
            + " ("
            + "{:.4f}".format(
                prev_user_event["parse_data"]["intent_ranking"][i + 1][
                    "confidence"
                ]
            )
            + ")\n"
        )
    return msg


class ActionLastIntent(Action):
    def name(self):
        print("ActionLastIntent self called")
        return "action_f1_score"

    def run(self, dispatcher, tracker, domain):
        # what your action should do
        msg = intentHistoryStr(tracker, 1, 4)
        dispatcher.utter_message(msg)  # send the message back to the user
        return []
