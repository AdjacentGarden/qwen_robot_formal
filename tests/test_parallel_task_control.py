import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_skills import LocalSkillBridge
from scenario_engine import ScenarioCatalog, ScenarioExecutor
from runtime_supervisor import InterruptibleTaskCoordinator, TaskSnapshot, TaskAction
from realtime_chat import RealtimeConversation, JsonLogger
from task_control import immediate_control

ROOT=Path(__file__).resolve().parents[1]

def success():
    return {'ok':True,'validation_ok':True,'executed':True,'spoken_summary':'完成'}

class CooperativeTests(unittest.TestCase):
    def test_cancel_navigation_skips_head_and_count_but_retains_cleanup(self):
        cancelled=False;calls=[]
        def invoke(name,args):
            nonlocal cancelled
            calls.append((name,args.get('action')))
            if name=='navigation_goto':cancelled=True
            return success()
        executor=ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'),invoke,
                                  cancellation_requested=lambda:cancelled)
        result=executor.execute('push_up_companion',{})
        self.assertNotIn(('head_control','up'),calls)
        self.assertFalse(any(name=='push_up' for name,_ in calls))
        self.assertEqual(calls[-1],('head_control','level'))
        self.assertFalse(result['ok'])

    def test_cancel_after_head_rolls_back_meeting_and_movie(self):
        for scene in ['meeting_projection','movie_projection']:
            with self.subTest(scene=scene):
                flag=threading.Event();calls=[]
                def invoke(name,args):
                    calls.append((name,args.get('action')))
                    if name=='head_control' and args.get('action')=='up':flag.set()
                    return success()
                ex=ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'),invoke,
                                    cancellation_requested=flag.is_set)
                result=ex.execute(scene,{'stay_put':True,'navigate':False})
                self.assertFalse(any(n=='navigation_goto' for n,_ in calls))
                self.assertFalse(any(n=='media_player' or a=='meeting_presentation_on' for n,a in calls))
                self.assertEqual(calls[-2:],[('projector_control','off'),('head_control','level')])
                self.assertFalse(result['ok'])

    def test_repeated_resume_preserves_original_duration_and_cleanup(self):
        c=InterruptibleTaskCoordinator()
        c.start(TaskSnapshot('push_up',{'duration':30},count=4,elapsed_seconds=12,
                             resume_prefix=(TaskAction('restore_context','head_control',{'direction':'up'}),)))
        for elapsed,remaining in [(12,18),(20,10),(27,3)]:
            c.checkpoint(elapsed_seconds=elapsed)
            c.interrupt('task_control',{'action':'pause'});c.interruption_completed()
            actions=c.resume_decision(True)
            resume=next(a for a in actions if a.kind=='resume_task')
            self.assertEqual(resume.arguments['duration'],remaining)
            self.assertEqual(resume.arguments['initial_elapsed_seconds'],elapsed)
            self.assertEqual(actions[-1].arguments,{'direction':'level'})
            self.assertEqual(actions[-2].name,'projector_control')

    def test_pause_fallback_does_not_steal_negative_history_or_media(self):
        for text in ['暂停一下','理想同学，先暂停运动','请暂停当前任务']:
            self.assertEqual(immediate_control(text,True)['arguments']['action'],'pause')
        for text in ['不要暂停','上次暂停运动做了几个','暂停会议画面','如果暂停会怎样','先暂停然后去书房']:
            self.assertIsNone(immediate_control(text,True))
        self.assertIsNone(immediate_control('暂停一下',False))

    def test_cancel_generation_is_not_reset_by_new_invocation(self):
        bridge=LocalSkillBridge(spec_dir=ROOT/'robot_skills/config/skill_specs',
                                enabled_skills=['head_control'],backend='subprocess',execute=False)
        records=[]
        def scoped(*args):
            records.append(bridge.execution_cancelled())
            if len(records)==1:
                bridge.cancel_all()
                records.append(bridge.execution_cancelled())
            return success()
        with patch.object(bridge,'_invoke_scoped',side_effect=scoped):
            bridge.invoke('head_control',{},turn_id='old')
            bridge.invoke('head_control',{},turn_id='new')
        self.assertEqual(records,[False,True,False])

    def test_speech_callback_carries_immutable_turn(self):
        events=[]
        bridge=LocalSkillBridge(spec_dir=ROOT/'robot_skills/config/skill_specs',
                                enabled_skills=['head_control'],event_callback=events.append)
        def invoke(*_args):
            bridge.current_turn_id='new'
            bridge._emit_speech_event({'kind':'acknowledgement','text':'测试'})
            return success()
        with patch.object(bridge,'_invoke_scoped',side_effect=invoke):
            bridge.invoke('head_control',{},turn_id='old')
        self.assertEqual(events[0]['turn_id'],'old')

    def test_final_scene_checkpoint_is_read_from_step_results(self):
        self.assertEqual(RealtimeConversation._extract_progress_from_result({
            'steps':[{'result':{'structured_result':{'count':6,'elapsed_seconds':13.5}}}]
        }),(6,13.5))

    def test_real_bridge_resume_cleanup_is_not_rewritten_as_meeting_scene(self):
        bridge=LocalSkillBridge(spec_dir=ROOT/'robot_skills/config/skill_specs',
            enabled_skills=['head_control','push_up','projector_control'],execute=False,
            scenario_catalog_path=ROOT/'scenarios/procedure_catalog.json')
        calls=[]
        def atomic(name,args,*_pos,**_kw):
            calls.append((name,args.get('action') or args.get('direction')))
            return success() if name!='push_up' else {**success(),'ok':False,'validation_ok':False,'error':'test_counter_failure'}
        tasks=[{'name':n,'arguments':a} for n,a in [
            ('head_control',{'direction':'up'}),
            ('push_up',{'action':'run','duration':10,'resume_from_interrupt':True}),
            ('projector_control',{'action':'off'}),
            ('head_control',{'direction':'level'})]]
        with patch.object(bridge,'_invoke_atomic',side_effect=atomic):
            result=bridge.invoke('run_skill_sequence',{'tasks':tasks,'failure_policy':'stop'},
                                 '继续刚才暂停的任务','resume','',True)
        self.assertEqual(calls,[('head_control','up'),('push_up','run'),('projector_control','off'),('head_control','level')])
        self.assertFalse(result['ok'])

class AsyncControls(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        c=RealtimeConversation(SimpleNamespace(), 'sk-test', JsonLogger(Path(temp.name)/'events.jsonl'))
        class Socket:
            async def send(self,_):pass
        c.websocket=Socket()
        return c

    async def test_pause_stops_worker_and_preserves_final_checkpoint(self):
        client=self.make_client();started=threading.Event();cancelled=threading.Event()
        class Bridge:
            current_turn_id='';scenario_catalog=None
            def recover_explicit_plan(self,*_):return None
            def recover_contextual_plan(self,*_):return None
            def cancel_all(self):cancelled.set()
            def invoke(self,*_):
                started.set();cancelled.wait(3)
                return {**success(),'structured_result':{'count':3,'elapsed_seconds':7,'state':'interrupted'}}
        client.skill_bridge=Bridge();client.user_turn_id=1
        old=client.schedule_function_call({'call_id':'old','name':'push_up','arguments':'{"duration":30}'})
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        client.user_turn_id=2
        task=client.schedule_function_call({'call_id':'pause','name':'task_control','arguments':'{"action":"pause"}'})
        await asyncio.gather(old,task)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(client.task_coordinator.state,'awaiting_resume')
        self.assertEqual(client.task_coordinator.suspended.count,3)
        self.assertEqual(client.task_coordinator.suspended.elapsed_seconds,7)

    async def test_here_scene_resume_does_not_add_navigation(self):
        client=self.make_client()
        client.skill_bridge=SimpleNamespace(scenario_catalog=ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'))
        # Same temporary no-navigation override used by the physical harness;
        # the formal fitness defaults remain unchanged.
        proc=client.skill_bridge.scenario_catalog.procedures['push_up_companion']
        proc['parameters']['navigate']={'type':'boolean','default':True}
        proc['steps'][0]['enabled_if']={'argument':'navigate','equals':True}
        snapshot=client._snapshot_from_call({'name':'run_robot_scenario','arguments':{
            'scenario':'push_up_companion','navigate':False,'stay_put':True,'duration':60}})
        self.assertIsNotNone(snapshot)
        self.assertIsNone(snapshot.location)

    async def test_stop_discards_suspended_checkpoint(self):
        client=self.make_client();c=client.task_coordinator
        c.start(TaskSnapshot('push_up',{'duration':30}));c.interrupt('task_control',{'action':'pause'});c.interruption_completed()
        result=await client._control_long_task({'action':'stop'})
        self.assertTrue(result['ok']);self.assertEqual(c.state,'idle');self.assertIsNone(c.suspended)

    async def test_resume_dispatch_uses_actual_head_action_schema(self):
        c=self.make_client();c.task_coordinator.start(TaskSnapshot('push_up',{'duration':30},
            resume_prefix=(TaskAction('restore_context','head_control',{'direction':'up'}),)))
        c.task_coordinator.interrupt('task_control',{'action':'pause'});c.task_coordinator.interruption_completed()
        with patch.object(c,'schedule_function_call') as schedule:
            await c._apply_resume_decision(True)
        tasks=json.loads(schedule.call_args.args[0]['arguments'])['tasks']
        heads=[t['arguments'] for t in tasks if t['name']=='head_control']
        self.assertEqual(heads,[{'action':'up'},{'action':'level'}])

    async def test_stop_acknowledgement_precedes_slow_worker_cleanup(self):
        c=self.make_client();started=threading.Event();cancelled=threading.Event();release=threading.Event()
        class Bridge:
            current_turn_id='';scenario_catalog=None
            def recover_explicit_plan(self,*_):return None
            def recover_contextual_plan(self,*_):return None
            def cancel_all(self):cancelled.set()
            def invoke(self,*_):started.set();release.wait(3);return success()
        c.skill_bridge=Bridge();c.user_turn_id=1
        old=c.schedule_function_call({'call_id':'old','name':'push_up','arguments':'{}'})
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        c.user_turn_id=2
        with patch.object(c,'_speak_internal') as speak:
            stop=c.schedule_function_call({'call_id':'cancel','name':'task_control','arguments':'{"action":"stop"}'})
            try:
                self.assertTrue(await asyncio.to_thread(cancelled.wait,1))
                self.assertFalse(old.done())
                self.assertIn('先停下',speak.call_args.args[1])
            finally:
                release.set();await asyncio.gather(old,stop)

    async def test_resumed_task_can_be_paused_again_without_losing_checkpoint(self):
        c=self.make_client();started=[threading.Event(),threading.Event()];release=[threading.Event(),threading.Event()]
        invocations=[]
        class Bridge:
            current_turn_id='';scenario_catalog=None
            def recover_explicit_plan(self,*_):return None
            def recover_contextual_plan(self,*_):return None
            def cancel_all(self):release[len(invocations)-1].set()
            def invoke(self,name,args,*_):
                index=len(invocations);invocations.append(dict(args));started[index].set();release[index].wait(3)
                return {**success(),'structured_result':{'count':3*(index+1),'elapsed_seconds':7*(index+1)}}
        c.skill_bridge=Bridge();c.user_turn_id=1
        old=c.schedule_function_call({'call_id':'first','name':'push_up','arguments':'{"duration":30}'})
        self.assertTrue(await asyncio.to_thread(started[0].wait,1))
        c.user_turn_id=2
        await c.schedule_function_call({'call_id':'pause1','name':'task_control','arguments':'{"action":"pause"}'})
        await old
        c.user_turn_id=3;await c._apply_resume_decision(True)
        self.assertTrue(await asyncio.to_thread(started[1].wait,1))
        c.user_turn_id=4
        await c.schedule_function_call({'call_id':'pause2','name':'task_control','arguments':'{"action":"pause"}'})
        self.assertEqual(c.task_coordinator.state,'awaiting_resume')
        self.assertEqual(c.task_coordinator.suspended.count,6)
        self.assertEqual(c.task_coordinator.suspended.elapsed_seconds,14)
        self.assertEqual(invocations[1]['duration'],23)

    async def test_memory_query_misclassified_by_model_does_not_cancel_running_task(self):
        client=self.make_client();started=threading.Event();release=threading.Event();cancels=[]
        class Bridge:
            current_turn_id='';scenario_catalog=None
            def recover_explicit_plan(self,*_):return None
            def recover_contextual_plan(self,*_):return None
            def cancel_all(self):cancels.append(True);release.set()
            def invoke(self,*_):started.set();release.wait(2);return success()
        client.skill_bridge=Bridge();client.user_turn_id=1
        old=client.schedule_function_call({'call_id':'running','name':'push_up','arguments':'{}'})
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        client.user_turn_id=2
        try:
            await client.schedule_function_call({'call_id':'history','name':'push_up','arguments':'{}'},
                                                user_text='我上一轮做了多少个俯卧撑？')
            self.assertEqual(cancels,[])
        finally:
            release.set();await old

    async def test_ack_and_gate_work_overlap_without_waiting_for_speech(self):
        loop=asyncio.get_running_loop();speech_started=asyncio.Event();speech_done=asyncio.Event()
        calls=[]
        async def slow_speech():
            speech_started.set();await asyncio.sleep(.15);speech_done.set()
        def callback(_):loop.call_soon_threadsafe(lambda:asyncio.create_task(slow_speech()))
        def invoke(name,args):
            calls.append((name,speech_done.is_set()))
            return success()
        ex=ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'),invoke,callback)
        await asyncio.to_thread(ex.execute,'meeting_projection',{'stay_put':True,'navigate':False})
        await speech_started.wait()
        self.assertFalse(calls[0][1]);await speech_done.wait()

if __name__=='__main__':unittest.main()
