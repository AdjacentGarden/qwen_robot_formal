from robot_graph.voice import TranscriptGate

def test_stale_and_partial_and_duplicate():
    g=TranscriptGate()
    assert g.handle({'type':'conversation.item.input_audio_transcription.completed','item_id':'old','transcript':'抬头'}) is None
    g.handle({'type':'input_audio_buffer.speech_started','item_id':'a'})
    assert g.handle({'type':'response.function_call_arguments.done','item_id':'a','arguments':'{}'}) is None
    assert g.handle({'type':'conversation.item.input_audio_transcription.delta','item_id':'a','transcript':'抬头'}) is None
    g.handle({'type':'input_audio_buffer.speech_started','item_id':'b'})
    assert g.handle({'type':'conversation.item.input_audio_transcription.completed','item_id':'a','transcript':'抬头'}) is None
    event={'type':'conversation.item.input_audio_transcription.completed','item_id':'b','transcript':'回正'}
    assert g.handle(event)[1]=='回正'
    assert g.handle(event) is None
