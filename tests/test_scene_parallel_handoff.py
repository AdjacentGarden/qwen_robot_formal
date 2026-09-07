"""No ROS, devices, or cloud API calls; real audio queue and scene executor."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from scenario_engine import ScenarioCatalog, ScenarioExecutor
from skill_event_audio import QwenSkillEventSpeaker

ROOT=Path(__file__).resolve().parents[1]

def success(executed=True):
    return {'ok':True,'validation_ok':True,'executed':executed}

class HandoffTests(unittest.TestCase):
    def run_scene(self, scene, arguments=None, fail_navigation=False, dry=False):
        timeline=[]
        def invoke(name, args):
            timeline.append(('action',name,args.get('action')))
            if fail_navigation and name=='navigation_goto':
                return {'ok':False,'validation_ok':False,'executed':True,'error':'no_path'}
            return success(not dry)
        def event(e): timeline.append(('speech',e['kind'],e['text']))
        result=ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'),invoke,
                                progress_callback=event).execute(scene,arguments or {})
        return result,timeline

    def test_arrival_progress_precedes_head_for_all_fitness_and_meeting(self):
        for scene in ['push_up_companion','squat_companion','pull_up_companion','meeting_projection']:
            with self.subTest(scene=scene):
                result,events=self.run_scene(scene)
                self.assertTrue(result['ok'])
                nav=next(i for i,e in enumerate(events) if e[:2]==('action','navigation_goto'))
                head=next(i for i,e in enumerate(events) if e==('action','head_control','up'))
                progress=[i for i,e in enumerate(events) if e[:2]==('speech','progress')]
                self.assertEqual(len(progress),1)
                self.assertLess(nav,progress[0]);self.assertLess(progress[0],head)
                self.assertNotIn('已经投影',events[progress[0]][2])

    def test_failed_navigation_does_not_announce_arrival_or_raise_head(self):
        for scene in ['push_up_companion','squat_companion','pull_up_companion','meeting_projection']:
            result,events=self.run_scene(scene,fail_navigation=True)
            self.assertFalse(result['ok'])
            self.assertFalse(any(e[:2]==('speech','progress') for e in events))
            self.assertNotIn(('action','head_control','up'),events)

    def test_stationary_meeting_does_not_claim_arrival(self):
        result,events=self.run_scene('meeting_projection',{'stay_put':True,'navigate':False})
        self.assertTrue(result['ok'])
        self.assertFalse(any(e[:2] in [('action','navigation_goto'),('speech','progress')] for e in events))
        self.assertEqual(sum(e[0]=='speech' for e in events),1)

    def test_dry_validation_does_not_claim_physical_arrival(self):
        for scene in ['push_up_companion','meeting_projection']:
            _,events=self.run_scene(scene,dry=True)
            self.assertFalse(any(e[:2]==('speech','progress') for e in events))

class ParallelQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_speech_can_remain_pending_while_hardware_steps_finish(self):
        # Freeze synthesis completely: submitting speech must not delay actions.
        with tempfile.TemporaryDirectory() as directory:
            speaker=QwenSkillEventSpeaker(api_key='not-used',voice='longanqian',workspace='',
                region='cn-beijing',endpoint='',connect_timeout=1,enqueue_pcm=lambda _:None,
                log=lambda *_a,**_k:None,cache_dir=Path(directory))
            speaker.loop=asyncio.get_running_loop()
            for scene in ['push_up_companion','meeting_projection']:
                head=threading.Event()
                def invoke(name,args):
                    if name=='head_control' and args.get('action')=='up':head.set()
                    return success()
                ex=ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'),invoke,
                                    progress_callback=speaker.submit_from_thread)
                result=await asyncio.wait_for(asyncio.to_thread(ex.execute,scene,{}),2)
                self.assertTrue(result['ok']);self.assertTrue(head.is_set())
            await asyncio.sleep(0)
            self.assertGreaterEqual(speaker.queue.qsize(),4)

if __name__=='__main__':unittest.main()
