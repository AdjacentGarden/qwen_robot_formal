"""Global audio readiness; counts expose no other session's dialogue."""


def audio_status(store):
    with store.lock:
        microphone = bool(store.one("SELECT task FROM leases WHERE resource='microphone'"))
        speaker = bool(store.one("SELECT task FROM leases WHERE resource='speaker'"))
        pending = store.one("SELECT count(*) AS n FROM outbox WHERE status IN ('pending','playing')")['n']
        accepted = store.one("SELECT count(*) AS n FROM operations WHERE kind IN ('speech.say','speaker.test') AND status='accepted'")['n']
        reason = ('microphone_busy' if microphone else
                  'speaker_busy_try_after_playback' if speaker or pending or accepted else None)
        return {'ready': reason is None, 'reason': reason, 'microphone_busy': microphone,
                'speaker_busy': speaker, 'pending_announcements': pending,
                'accepted_playbacks': accepted}
