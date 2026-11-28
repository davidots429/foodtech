"""C# generated controller points reach Python unchanged, never interpolated."""
import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from foodtech_contracts.tonkatsu_controller_samples import observe_controller

ROOT=Path(__file__).resolve().parents[2]
DLL=ROOT/'apps/mock-control/bin/Release/net10.0/MockControl.dll'
FIXTURE=ROOT/'fixtures/tonkatsu-cooking-normal-v1.json'

@unittest.skipUnless(shutil.which('dotnet') and DLL.exists(),'built .NET 10 Mock required')
class TonkatsuCookingMockTests(unittest.TestCase):
    def generate(self,root,fixture=None):
        source,output=root/'fixture.json',root/'generated.json'
        source.write_text(json.dumps(fixture or json.loads(FIXTURE.read_text())))
        command=['dotnet',str(DLL),'simulate-tonkatsu-cooking','--fixture',str(source),'--output',str(output)]
        result=subprocess.run(command,capture_output=True,timeout=10)
        return result,output,command
    def observe(self,records):
        placed=datetime(2026,11,28,tzinfo=timezone.utc)+timedelta(seconds=10)
        return observe_controller(records,placed=placed,lifted=placed+timedelta(seconds=300))

    def test_generated_one_hz_points_preserve_recovery_and_do_not_overwrite_previous_output(self):
        with tempfile.TemporaryDirectory() as folder:
            result,output,command=self.generate(Path(folder));self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(output.read_text(),parse_float=Decimal);records=data['records']
            self.assertEqual(len(records),63)
            summary=self.observe(records)
            self.assertEqual(summary.sample_count,301);self.assertTrue(summary.calibration_all_ok)
            self.assertEqual((summary.preplacement_sample_c,summary.preplacement_age_us),(Decimal(170),1000000))
            self.assertTrue(summary.preplacement_calibration_ok)
            self.assertEqual(summary.recoveries,((1000000,31000000,36000000),))
            self.assertEqual((summary.minimum_sample_c,summary.maximum_sample_c),(Decimal(164),Decimal(170)))
            before=output.read_bytes()
            self.assertNotEqual(subprocess.run(command,capture_output=True,timeout=10).returncode,0)
            self.assertEqual(output.read_bytes(),before)
            other=Path(folder)/'repeat.json';subprocess.run([*command[:-1],str(other)],check=True,capture_output=True,timeout=10)
            self.assertEqual(other.read_bytes(),before)

    def test_internal_spike_is_preserved_when_the_five_second_envelope_is_normal(self):
        with tempfile.TemporaryDirectory() as folder:
            result,output,_=self.generate(Path(folder));self.assertEqual(result.returncode,0)
            records=json.loads(output.read_text(),parse_float=Decimal)['records']
            records[12]['extensions']['tonkatsuSamples']['samples'][1]['oilTemperatureCelsius']=185
            summary=self.observe(records)
            self.assertEqual(summary.maximum_sample_c,185)
            self.assertEqual(records[12]['measurements']['temperatureCelsius'],170)
            self.assertEqual(summary.above_180_sample_span_seconds,0)

    def test_partial_changed_reordered_or_unbracketed_controller_samples_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            result,output,_=self.generate(Path(folder));self.assertEqual(result.returncode,0)
            original=json.loads(output.read_text(),parse_float=Decimal)['records']
            for kind in ('missing','time','second','last-temperature','sample-count','order','bracket'):
                records=copy.deepcopy(original)
                if kind=='missing':records[1]['extensions'].pop('tonkatsuSamples')
                elif kind=='time':records[1]['extensions']['tonkatsuSamples']['samples'][0]['observedAt']='2026-11-28T00:00:00Z'
                elif kind=='second':records[1]['extensions']['tonkatsuSamples']['samples'][0]['second']=5
                elif kind=='last-temperature':records[1]['extensions']['tonkatsuSamples']['samples'][-1]['oilTemperatureCelsius']=169
                elif kind=='sample-count':records[1]['extensions']['tonkatsuSamples']['samples'].pop()
                elif kind=='order':records[1],records[2]=records[2],records[1]
                else:records=records[:-2]
                with self.assertRaises(ValueError):self.observe(records)
            for row in original:row['extensions'].pop('tonkatsuSamples')
            self.assertIsNone(self.observe(original))

    def test_calibration_failure_is_preserved_and_never_relabelled_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture=json.loads(FIXTURE.read_text());fixture['segments'][1]['calibrationStatus']='EXPIRED'
            result,output,_=self.generate(Path(folder),fixture);self.assertEqual(result.returncode,0)
            summary=self.observe(json.loads(output.read_text(),parse_float=Decimal)['records'])
            self.assertFalse(summary.calibration_all_ok)

    def test_invalid_generator_inputs_do_not_create_output(self):
        for change in ('duration','bounds','provenance','time','seed'):
            with tempfile.TemporaryDirectory() as folder:
                fixture=json.loads(FIXTURE.read_text())
                if change=='duration':fixture['segments'][0]['seconds']=9
                elif change=='bounds':fixture['segments'][0]['seconds']=5001
                elif change=='provenance':fixture['provenance']='VERIFIED'
                elif change=='time':fixture['logicalStartAt']='2026-11-28T00:00:00+01:00'
                else:fixture['seed']=-1
                result,output,_=self.generate(Path(folder),fixture)
                self.assertNotEqual(result.returncode,0);self.assertFalse(output.exists())

    def test_generated_controller_and_product_probes_reach_the_same_recorded_job_observation(self):
        from uuid import uuid4
        from foodtech_contracts import stream_binding_key
        from foodtech_contracts.tonkatsu_batch import batch
        from foodtech_contracts.tonkatsu_observed_inputs import observe_tonkatsu_batch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);result,output,_=self.generate(root);self.assertEqual(result.returncode,0)
            records=json.loads(output.read_text())['records']
            start=datetime(2026,11,28,tzinfo=timezone.utc)
            iso=lambda second:(start+timedelta(seconds=second)).isoformat()
            probes=json.loads((ROOT/'fixtures/tonkatsu-probes-normal-v1.json').read_text())
            probes['measurementStart']=iso(310)
            probe_source,probe_output=root/'probes-in.json',root/'probes-out.json'
            probe_source.write_text(json.dumps(probes))
            subprocess.run(['dotnet',str(DLL),'simulate-tonkatsu-probes','--fixture',str(probe_source),
                            '--output',str(probe_output)],check=True,capture_output=True,timeout=10)
            generated=json.loads(probe_output.read_text(),parse_float=Decimal)
            job={'job_id':str(uuid4()),'menu':'TONKATSU','provenance':'SYNTHETIC','status':'COMPLETED',
                'equipment_profile':'synthetic-fryer-approved-v2','thickness_profile':'STANDARD','thickness_cm':'1.25',
                'recipe_version':'synthetic-tonkatsu-baseline-v1',
                'actual_cooking_seconds':300,'actual_placed_at':iso(10),'actual_lifted_at':iso(310),
                'tonkatsu_batch':batch('STANDARD',[160]*4,'PARTIAL'),'tonkatsu_batch_version':1,'actual_load_kg':'0.64',
                'safety_status':'PASSED','safety_version':1,'core_check_safety_version':1,'core_check':generated['probes'],
                'source_binding':{'source_id':'synthetic-rest','device_id':'mockprofile-100-a-001',
                                  'batch_id':'batch-a-tonkatsu-normal'},
                'tonkatsu_initial_context':{'schema_version':'tonkatsu-initial-context-v1','provenance':'SYNTHETIC',
                    'status':'DECLARED_UNVERIFIED','batch_version':1,'product_classification':'NON_MINCED_PORK_LOIN_RAW',
                    'ingredient_temperature_c':'4','temperature_observed_at':iso(0),'storage_state':'CHILLED',
                    'oil_capacity_l':'10','oil_batches_since_change':0,'oil_cooked_mass_kg':'0','oil_changed_at':iso(-100)}}
            rows=[{'pk':stream_binding_key(**job['source_binding']),'sk':f'RAW#{index:06d}',
                   'provenance':'SYNTHETIC','raw':json.dumps(record)} for index,record in enumerate(records,1)]
            observed=observe_tonkatsu_batch(job,rows)
            self.assertEqual(observed.unresolved,());self.assertTrue(observed.core_series_consistent)
            self.assertEqual(observed.reference_recipe_version,job['recipe_version'])
            self.assertEqual(observed.preplacement_age_us,1000000)
            self.assertEqual(observed.controller_summary.recoveries,((1000000,31000000,36000000),))
            self.assertEqual(observed.model_context()['initial_context_status'],'DECLARED_UNVERIFIED')
