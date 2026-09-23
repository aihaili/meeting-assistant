"""Live meeting assistant: the UI, the realtime audio path, and clue extraction.

Layout::

    sound card (sounddevice) --> LocalMicReceiver
                                        |
                              sliding-window ASR (utterances)
                                        |
                                     MeetingService.on_segment  --+--> Assistant
                                                            |         (hotspots, retrieval)
                                                            +--> classify
                                                                      (typed clues)
                                                            |
                                                  MeetingSession (JSON, autosaved)
                                                            |
                                              HTTP :8510 --> ui.html (three columns)

The three columns answer the three questions a person actually has while listening:
who is in the room (left), what was just said (centre), and what in it matters and on
what evidence (right). Everything else in this package exists to keep those three
correct.
"""

from .session import (ACTIONABLE, CLUE_TYPES, Clue, MeetingSession, Participant,  # noqa: F401
                      PlanItem, type_meta)

__all__ = ["ACTIONABLE", "CLUE_TYPES", "Clue", "MeetingSession", "Participant",
           "PlanItem", "type_meta"]
