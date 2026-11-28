"""Pure recorded facts; no SDK, model calls, permissions or attestation."""
import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4
from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_batch import batch
from foodtech_contracts.tonkatsu_observed_inputs import observe_tonkatsu_batch


class ObservedTonkatsuTests(unittest.TestCase):
    def setUp(self):
        placed=datetime(2026,11,28,tzinfo=timezone.utc)
        self.iso=lambda seconds:(placed+timedelta(seconds=seconds)).isoformat()
        self.job={'job_id':str(uuid4()),'owner_subject':'private-owner-marker','notes':'private-notes-marker',
            'menu':'TONKATSU','provenance':'SYNTHETIC','equipment_profile':'synthetic-fryer-approved-v2',
            'status':'COMPLETED','recipe_version':'private-recipe-version',
            'process_assessment_status':'NORMAL','thickness_profile':'STANDARD','thickness_cm':'1.25',
            'actual_cooking_seconds':Decimal(300),'actual_placed_at':self.iso(0),'actual_lifted_at':self.iso(300),
            'tonkatsu_batch':batch('STANDARD',[160,160],'PARTIAL'),'tonkatsu_batch_version':Decimal(2),
            'actual_load_kg':'0.32','safety_status':'PASSED','safety_version':Decimal(3),'core_check_safety_version':Decimal(3),
            'core_check':[{'item_id':str(i),'calibration_status':'OK','probe_id':f'synthetic-probe-{i}',
                           'removal_sequence':Decimal(i+1),'measurement_start':self.iso(300),'measurement_end':self.iso(360),
                           'samples':[{'at_second':Decimal(s),'celsius':'75'} for s in range(61)]} for i in range(2)],
            'source_binding':{'source_id':'synthetic-rest','device_id':'mockprofile-100-a-001','batch_id':'batch-a-observed'},
            'tonkatsu_initial_context':{'schema_version':'tonkatsu-initial-context-v1','provenance':'SYNTHETIC',
                'status':'DECLARED_UNVERIFIED','batch_version':Decimal(2),'product_classification':'NON_MINCED_PORK_LOIN_RAW',
                'ingredient_temperature_c':'4','temperature_observed_at':self.iso(-15),'storage_state':'CHILLED',
                'oil_capacity_l':'10','oil_batches_since_change':Decimal(1),'oil_cooked_mass_kg':'2',
                'oil_changed_at':self.iso(-86400),'notes':'private-oil-note'}}
        self.job['tonkatsu_batch']['product_weights_g']=[Decimal(160),Decimal(160)]
        self.raws=[]
        for sequence,second in enumerate(range(-5,306,5),1):
            record={'schemaVersion':'1.0','sourceId':'synthetic-rest','deviceId':'mockprofile-100-a-001',
                'batchId':'batch-a-observed','recordId':f'rec-a-observed-{sequence}','sequenceNumber':sequence,
                'eventTime':self.iso(second),'deviceStatus':'running','extensions':{'private':'private-extension'},
                'measurements':{'temperatureCelsius':170 if second<0 else 169,'speedRpm':0,'elapsedSeconds':second+5}}
            self.raws.append({'pk':stream_binding_key(**self.job['source_binding']), 'sk':f'RAW#{sequence:06d}',
                              'provenance':'SYNTHETIC','raw':json.dumps(record)})
    def observe(self):return observe_tonkatsu_batch(self.job,self.raws)
    def change(self,index,**kwargs):
        value=json.loads(self.raws[index]['raw']);value.update(kwargs);self.raws[index]['raw']=json.dumps(value)

    def test_actual_duration_load_point_samples_and_declared_values_are_preserved_without_identity(self):
        value=self.observe()
        self.assertEqual(value.unresolved,('EXPLICIT_1HZ_CONTROLLER_SAMPLES_MISSING',))
        self.assertEqual((value.sample_count,value.cooking_seconds,value.load_kg),(61,300,Decimal('.32')))
        self.assertEqual((value.preplacement_sample_c,value.preplacement_age_us),(Decimal(170),5000000))
        self.assertEqual((value.as_baseline().profile,value.as_baseline().previous_duration_seconds),('STANDARD',300))
        context=value.model_context()
        self.assertEqual(context['initial_context_status'],'DECLARED_UNVERIFIED')
        self.assertNotIn('completeness',context);self.assertNotIn('critical_data_valid',context)
        self.assertEqual(context['batch'], {
            'product_classification':'NON_MINCED_PORK_LOIN_RAW','thickness_cm':'1.25',
            'product_weights_g':[160,160],'product_count':2,'batch_mode':'PARTIAL',
            'batch_rule_version':'tonkatsu-batch-v2','rated_capacity_kg':'4.0','load_percent':'8.00',
            'maximum_product_count':15,'target_product_weight_g':160,'product_weight_tolerance_g':5,
            'maximum_batch_mass_kg':'2.5',
        })
        self.assertEqual(context['recorded_process_status'], 'NORMAL')
        self.assertEqual(context['cooking_time_status'], 'NORMAL')
        self.assertEqual(context['core_observation'], {
            'probe_count':2,'minimum_celsius':'75','probes_below_limit':0,'observed_span_seconds':60,'samples_per_probe':61,
        })
        for marker in ('private-owner','private-notes','private-oil','private-extension','private-recipe','synthetic-probe',self.job['job_id']):
            self.assertNotIn(marker,json.dumps(context));self.assertNotIn(marker,repr(value))

    def test_missing_recipe_invalid_cold_state_and_arbitrary_process_label(self):
        self.job['recipe_version'] = '  '
        self.job['tonkatsu_initial_context']['storage_state'] = 'FROZEN'
        self.job['process_assessment_status'] = 'private-arbitrary-process'
        result = self.observe()
        self.assertIn('REFERENCE_RECIPE_VERSION_UNAVAILABLE', result.unresolved)
        self.assertIn('STORAGE_OUTSIDE_SYNTHETIC_SCENARIO', result.unresolved)
        self.assertEqual(result.recorded_process_status, 'NOT_RECORDED')
        self.assertNotIn('private-arbitrary-process', str(result.model_context()))

    def test_missing_or_stale_initial_context_is_never_filled_from_defaults(self):
        self.job['tonkatsu_initial_context']['batch_version']=1
        value=self.observe()
        self.assertIsNone(value.ingredient_temperature_c);self.assertIsNone(value.oil_capacity_l)
        self.assertEqual(value.storage_state,'UNKNOWN');self.assertIn('INITIAL_CONTEXT_MISSING_OR_STALE',value.unresolved)

    def test_unknown_fields_future_initial_values_and_invalid_numeric_inputs_remain_unresolved(self):
        initial=self.job['tonkatsu_initial_context'];initial['temperature_observed_at']=self.iso(1)
        initial['oil_capacity_l']='NaN';initial['oil_batches_since_change']=True;initial['storage_state']='unknown-private-text'
        value=self.observe()
        self.assertIsNone(value.oil_capacity_l);self.assertIsNone(value.oil_batches_since_change)
        self.assertIn('TEMPERATURE_OBSERVED_AT_UNAVAILABLE',value.unresolved)
        self.assertNotIn('unknown-private-text',str(value.model_context()))

    def test_all_core_probes_and_current_safety_version_are_required(self):
        for kind in ('missing','calibration','duplicate','sample-gap','version','nonfinite','probe-id','time'):
            self.setUp()
            if kind=='missing':self.job['core_check'].pop()
            elif kind=='calibration':self.job['core_check'][0]['calibration_status']='EXPIRED'
            elif kind=='duplicate':self.job['core_check'][1]['item_id']='0'
            elif kind=='sample-gap':self.job['core_check'][0]['samples'][1]['at_second']=2
            elif kind=='version':self.job['core_check_safety_version']=2
            elif kind=='probe-id':self.job['core_check'][0]['probe_id']=None
            elif kind=='time':self.job['core_check'][0]['measurement_start']=self.iso(299)
            else:self.job['core_check'][0]['samples'][1]['celsius']='Infinity'
            value=self.observe();self.assertFalse(value.core_series_consistent)
            self.assertIn('CORE_SERIES_MISSING_STALE_OR_INCONSISTENT',value.unresolved)

    def test_recorded_failure_requires_a_measured_below_threshold_and_is_not_hidden(self):
        self.job['safety_status']='FAILED'
        self.assertFalse(self.observe().core_series_consistent)
        self.job['core_check'][1]['samples'][30]['celsius']='74.9'
        value=self.observe();self.assertTrue(value.core_series_consistent)
        self.assertEqual(value.core_summary.minimum_celsius, Decimal('74.9'))
        self.assertEqual(value.core_summary.probes_below_limit, 1)
        self.assertEqual(value.recorded_safety_status,'FAILED');self.assertEqual(value.unresolved,('EXPLICIT_1HZ_CONTROLLER_SAMPLES_MISSING',))

    def test_missing_preplacement_and_interval_end_are_reported_without_temperature_interpolation(self):
        self.raws=self.raws[1:-2]
        value=self.observe()
        self.assertIsNone(value.preplacement_sample_c);self.assertIsNone(value.preplacement_age_us)
        self.assertIn('PREPLACEMENT_TEMPERATURE_NOT_CAPTURED',value.unresolved)
        self.assertIn('SOURCE_DOES_NOT_BRACKET_COOKING_INTERVAL',value.unresolved)

    def test_gapped_cadence_or_stopped_device_cannot_become_complete(self):
        self.raws.pop(3);self.change(1,deviceStatus='stopped')
        value=self.observe()
        self.assertIn('SOURCE_SEQUENCE_OR_CADENCE_GAP',value.unresolved)
        self.assertIn('SOURCE_DEVICE_NOT_RUNNING_OR_ERROR',value.unresolved)

    def test_reheat_is_not_mistaken_for_original_cooking_and_safety_context(self):
        self.job['latest_tonkatsu_reheat']={'private':'not-for-model'}
        self.assertIn('REHEAT_TIMELINE_REQUIRES_SEPARATE_OBSERVATION',self.observe().unresolved)

    def reheat_fixture(self):
        self.job["pk"] = "scoped-job"

        def event(version, trigger, before, after, time, **fields):
            return {"pk": self.job["pk"], "sk": f"EVENT#SAFETY#{version:012d}", "domain": "SAFETY_STATUS",
                    "trigger": trigger, "from_status": before, "to_status": after,
                    "occurred_at": self.iso(time), **fields}

        def probes(lifted, temperature):
            rows = copy.deepcopy(self.job["core_check"])
            for row in rows:
                row["measurement_start"] = self.iso(lifted)
                row["measurement_end"] = self.iso(lifted + 60)
            rows[0]["samples"][30]["celsius"] = temperature
            return rows

        first = {"segment_id": str(uuid4()), "item_ids": ["0", "1"], "started_at": self.iso(371),
                 "lifted_at": self.iso(421), "duration_seconds": "50.0", "previous_safety_version": Decimal(3),
                 "reason": "private-reheat-reason"}
        second = {**first, "segment_id": str(uuid4()), "started_at": self.iso(490),
                  "lifted_at": self.iso(515), "duration_seconds": "25", "previous_safety_version": Decimal(6)}
        final = probes(515, "76")
        events = [
            event(3, "CORE_CHECK", "NOT_MEASURED", "FAILED", 365, probes=probes(300, "74"), reheat_segment_id=None),
            event(4, "TONKATSU_REHEATED", "FAILED", "INCOMPLETE", 425, segment=first),
            event(5, "CORE_CHECK", "INCOMPLETE", "INCOMPLETE", 482, probes=probes(421, None),
                  reheat_segment_id=first["segment_id"]),
            event(6, "CORE_CHECK", "INCOMPLETE", "FAILED", 483, probes=probes(421, "74.9"),
                  reheat_segment_id=first["segment_id"]),
            event(7, "TONKATSU_REHEATED", "FAILED", "INCOMPLETE", 520, segment=second),
            event(8, "CORE_CHECK", "INCOMPLETE", "PASSED", 580, probes=final, reheat_segment_id=second["segment_id"]),
        ]
        self.job.update(safety_version=Decimal(8), core_check_safety_version=Decimal(8), core_check=copy.deepcopy(final),
                        latest_tonkatsu_reheat=copy.deepcopy(second), pending_tonkatsu_reheat=None)
        return events

    def test_two_reheats_preserve_original_failure_and_final_assessment(self):
        result = observe_tonkatsu_batch(self.job, self.raws, safety_events=self.reheat_fixture())
        history = result.model_context()["reheat_history"]
        self.assertEqual(history["initial_safety_status"], "FAILED")
        self.assertEqual(history["initial_core_observation"]["minimum_celsius"], "74")
        segments = history["additional_heat_segments"]
        self.assertEqual([(x["duration_seconds"], x["incomplete_rechecks"], x["safety_status"]) for x in segments],
                         [("50", 1, "FAILED"), ("25", 0, "PASSED")])
        self.assertEqual(result.recorded_safety_status, "PASSED")
        self.assertTrue(result.core_series_consistent)
        self.assertEqual(result.core_summary.minimum_celsius, Decimal(75))
        self.assertEqual(result.cooking_seconds, 300)
        self.assertNotIn("REHEAT_TIMELINE_REQUIRES_SEPARATE_OBSERVATION", result.unresolved)
        self.assertNotIn("private-reheat-reason", str(result.model_context()))
        self.assertNotIn(self.job["latest_tonkatsu_reheat"]["segment_id"], str(result.model_context()))

    def test_inconsistent_reheat_history_blocks_observation(self):
        for kind in ("gap", "foreign", "duration", "wrong_products", "stale_latest", "pending", "updated_core"):
            self.setUp()
            events = self.reheat_fixture()
            if kind == "gap":
                del events[2]
            elif kind == "foreign":
                events[4]["pk"] = "another-job"
            elif kind == "duration":
                events[1]["segment"]["duration_seconds"] = "49"
            elif kind == "wrong_products":
                events[5]["probes"][0]["item_id"] = "replacement"
            elif kind == "stale_latest":
                self.job["latest_tonkatsu_reheat"] = events[1]["segment"]
            elif kind == "pending":
                self.job["pending_tonkatsu_reheat"] = self.job["latest_tonkatsu_reheat"]
            elif kind == "updated_core":
                self.job["core_check"][1]["samples"][1]["celsius"] = "76"
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "^synthetic fryer observation unavailable$"):
                observe_tonkatsu_batch(self.job, self.raws, safety_events=events)

    def test_inconsistent_job_binding_thickness_time_load_or_foreign_raw_is_rejected(self):
        for kind in ('thickness','time','load','batch','duplicate','foreign','protocol','nonfinite'):
            self.setUp()
            if kind=='thickness':self.job['thickness_cm']='0.8'
            elif kind=='time':self.job['actual_cooking_seconds']=300.1
            elif kind=='load':self.job['actual_load_kg']='.5'
            elif kind=='batch':self.change(0,batchId='batch-a-other')
            elif kind=='duplicate':self.raws[1]=copy.deepcopy(self.raws[0])
            elif kind=='foreign':self.raws[0]['pk']='foreign-private'
            elif kind=='protocol':self.job['source_binding']['device_id']='mockprofile-110-a-001'
            else:self.change(0,measurements={'temperatureCelsius':float('nan'),'speedRpm':0,'elapsedSeconds':0})
            with self.assertRaisesRegex(ValueError,'^synthetic fryer observation unavailable$'):self.observe()

    def test_scoped_source_reader_matches_receipt_and_rejects_revision_race(self):
        from unittest.mock import Mock
        from foodtech_contracts import payload_hash
        from foodtech_contracts.collection_evidence import CompletionTracker
        from foodtech_contracts.tonkatsu_source_quality import quality_evidence
        from foodtech_ai_gateway.tonkatsu_source_observation import observe_scoped_tonkatsu_source

        tracker = CompletionTracker(0, "mockprofile-100", "mockprofile-100-a-001")
        stream = self.raws[0]["pk"]
        evidence = []
        for raw in self.raws:
            event = str(uuid4())
            raw.update(sk=f"RAW#{event}", ingestion_event_id=event,
                       source_id="synthetic-rest", device_id="mockprofile-100-a-001")
            record = json.loads(raw["raw"])
            record["extensions"]["tonkatsuSamples"] = {
                "schemaVersion": "tonkatsu-controller-samples-v1", "sampleIntervalSeconds": 1,
                "samples": [
                    {"second": (record["sequenceNumber"] - 1) * 5 + index + 1,
                     "observedAt": self.iso(record["measurements"]["elapsedSeconds"] - 9 + index),
                     "oilTemperatureCelsius": record["measurements"]["temperatureCelsius"],
                     "calibrationStatus": "OK"} for index in range(5)
                ],
            }
            raw["raw"] = json.dumps(record)
            tracker.record(raw["raw"], record)
            evidence.append(quality_evidence(raw, {
                "pk": f"EVENT#{event}", "sk": "ASSESSMENT#v1", "source_pk": stream,
                "raw_sk": raw["sk"], "ingestion_event_id": event, "payload_hash": payload_hash(record),
                "quality_status": "VALID", "dedup_status": "UNIQUE", "finding_codes": [],
            }))
        count = len(self.raws)
        receipt = {"pk": stream, "sk": "COLLECTION#RECEIPT#test",
                   **tracker.finish(next_cursor=count, complete=True, completion_explicit=True)}
        raws, markers = [sorted(rows, key=lambda row: row["sk"]) for rows in (self.raws, evidence)]
        table = Mock(name="data")
        revision = {"Item": {"pk": stream, "sk": "SOURCE#REVISION", "revision": Decimal(count)}}
        paginator = table.meta.client.get_paginator.return_value

        for mode in ("complete", "changed", "missing", "foreign"):
            with self.subTest(mode=mode):
                table.get_item.side_effect = [
                    revision,
                    revision if mode != "changed" else {"Item": {**revision["Item"], "revision": count + 1}},
                ]
                items = markers if mode != "missing" else markers[:-1]
                raw_page = raws if mode != "foreign" else [{**raws[0], "pk": "foreign"}]
                paginator.paginate.side_effect = [
                    iter([{"Items": raw_page}]), iter([{"Items": [receipt]}]), iter([{"Items": items}]),
                ]
                request = dict(table=table, job=self.job, scoped_stream=stream, expected_revision=count)
                if mode == "complete":
                    result = observe_scoped_tonkatsu_source(**request)
                    self.assertEqual(result.unresolved, ())
                    self.assertEqual(result.preplacement_age_us, 1_000_000)
                else:
                    with self.assertRaises(ValueError):
                        observe_scoped_tonkatsu_source(**request)
