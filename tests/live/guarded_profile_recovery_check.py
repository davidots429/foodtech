"""Validate guarded deletion transaction races using isolated synthetic dev items.

Existing development tables stand in for four separate security domains. This
does not verify production IAM, dedicated privacy resources or a deletion saga.
"""
import base64
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from foodtech_business_api.capsule_writer import CapsuleWriter
from foodtech_business_api.capsule_inventory import CapsuleInventory
from foodtech_business_api.guarded_deletion import GuardedDeletion, VaultSnapshot
from foodtech_business_api.guarded_deletion_recovery import GuardedDeletionRecovery
from foodtech_business_api.deletion_registry_cipher import DeletionRegistryCipher
from foodtech_business_api.deletion_registry_recovery_cipher import DeletionRegistryRecoveryCipher
from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.user_entry import seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator
from foodtech_capsule_crypto.deletion_plan import prepare_from_resolved_inventory
from foodtech_capsule_crypto.plan_worker import CapsulePlanWorker
from foodtech_business_api.profile_deletion_inventory import ProfileDeletionInventory
from foodtech_business_api.profile_deletion_worker import ProfileDeletionWorker
from foodtech_business_api.profile_deletion_residual import ProfileResidualScan
from foodtech_business_api.profile_deletion_coordinator import ProfileDeletionCoordinator
from foodtech_business_api.profile_deletion_recovery import ProfileDeletionRecovery
from foodtech_business_api.job_deletion_inventory import JobDeletionInventory
from foodtech_business_api.job_deletion_worker import JobDeletionWorker
from foodtech_business_api.job_binding_inventory import JobBindingInventory
from foodtech_business_api.job_deletion_residual import JobResidualScan, JobFinalResidualScan
from foodtech_business_api.job_binding_residual import JobBindingResidualScan, JobBindingFinalResidualScan
from foodtech_business_api.job_deletion_coordinator import JobDeletionCoordinator
from foodtech_business_api.job_deletion_recovery import JobDeletionRecovery
from foodtech_business_api.job_meta_deletion import JobMetaDeletion
from foodtech_business_api.job_source_snapshot import JobSourceSnapshot
from foodtech_business_api.meal_deletion_coordinator import MealDeletionCoordinator
from foodtech_business_api.meal_deletion_inventory import MealDeletionInventory
from foodtech_business_api.meal_deletion_recovery import MealDeletionRecovery
from foodtech_business_api.meal_deletion_residual import MealFinalResidualScan, MealResidualScan
from foodtech_business_api.meal_deletion_worker import MealDeletionWorker
from foodtech_business_api.meal_meta_deletion import MealMetaDeletion
from foodtech_business_api.meal_source_snapshot import MealSourceSnapshot
from foodtech_business_api.cooking_deletion_recovery import CookingDeletionRecovery
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_capsule_crypto.tenant_scope import TenantScope, TenantScopeResolver
from process_flow_dynamodb_check import ACCOUNT, REGION, remove_partition


def b64(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


AUTH = "foodtech-auth-sessions-dev"
VAULT = "foodtech-cooking-jobs-dev"
CAPSULE = "foodtech-data-flow-dev"
REGISTRY = "foodtech-ai-execution-registry-dev"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_PROFILE_RECOVERY_LIVE") != "1":
        raise SystemExit("Use AWS_PROFILE=foodtech and FOODTECH_PROFILE_RECOVERY_LIVE=1")
    if not ProfileDeletionRecovery._writer_quiescence_proven():
        raise SystemExit('Live cleanup requires verified legacy writer quiescence')
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    client, dynamo = session.client("dynamodb"), session.resource("dynamodb")
    outputs = session.client("cloudformation").describe_stacks(StackName="FoodtechCredentialVault-dev")["Stacks"][0]["Outputs"]
    key_arn = next(item["OutputValue"] for item in outputs if item["OutputKey"] == "CredentialStorageKeyArn")
    hmac_arn = next(item["OutputValue"] for item in outputs if item["OutputKey"] == "CredentialPseudonymKeyArn")
    encryptor = DeletionRegistryCipher(key_arn, "dev", kms_client=session.client("kms"))
    decryptor = DeletionRegistryRecoveryCipher(key_arn, "dev", kms_client=session.client("kms"))
    auth, vault, capsule, registry = (dynamo.Table(name) for name in (AUTH, VAULT, CAPSULE, REGISTRY))
    progress_table = dynamo.Table("foodtech-ai-credential-vault-dev")
    subject, vault_id, internal_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    stale_id, stale_capsule_id, first_id, second_id = (str(uuid.uuid4()) for _ in range(4))
    ids = (stale_id, stale_capsule_id, first_id, second_id)
    auth_key = {"pk": f"USER#{subject}"}
    vault_key = {"pk": vault_id, "sk": "META"}
    writer = CapsuleWriter(CAPSULE, frozenset({"BILLING"}), client=client)
    locator, create_action = writer.create_action()
    if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item") or \
            vault.query(KeyConditionExpression=Key("pk").eq(vault_id), ConsistentRead=True).get("Items") or \
            capsule.query(KeyConditionExpression=Key("pk").eq(locator), ConsistentRead=True).get("Items") or \
            any(registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{value}"},
                                  ConsistentRead=True).get("Item") for value in ids) or \
            any(progress_table.get_item(Key={"pk": f"REQ#{value}", "sk": f"DOMAIN#{domain}"},
                                        ConsistentRead=True).get("Item")
                for value in ids for domain in ("CAPSULE_PLAN", "CAPSULE_PLAN_PAGE_000000000001")):
        raise SystemExit("Synthetic identifiers already exist")
    auth_created = vault_created = capsule_created = False
    plan_request = None
    profile_created = []
    job_created = []
    meal_created = []
    try:
        auth.put_item(Item={**auth_key, "epoch": 0, "vault_id": vault_id, "provenance": "SYNTHETIC"},
                      ConditionExpression=Attr("pk").not_exists())
        auth_created = True
        vault.put_item(Item={**vault_key, "state": "ACTIVE", "revision": 2,
                             "capsule_locator": locator, "internal_user_id": internal_id, "provenance": "SYNTHETIC"},
                       ConditionExpression=Attr("pk").not_exists())
        vault_created = True
        client.transact_write_items(TransactItems=[create_action])
        capsule_created = True
        key_map = {('synthetic-v1', 'shard-0'): key_arn}
        purposes, tenant_purposes = frozenset({'BILLING'}), frozenset({'credential'})
        resolver = TenantScopeResolver(key_map={('credential', 'v1'): hmac_arn},
            approved_purposes=tenant_purposes, kms_client=session.client('kms'))
        identity_buffer = bytearray(internal_id.encode('ascii'))
        try:
            tenant = resolver.derive(internal_id=identity_buffer, scope=TenantScope('credential', '2026-11', 'v1'))
        finally:
            identity_buffer[:] = b'\x00' * len(identity_buffer)
        target = Entry('BILLING', '2026-11', b64(2))
        user_key, entry = UserKeyGenerator(key_map, tenant_purposes, kms_client=session.client('kms')).generate(
            tenant=tenant, pool_version='synthetic-v1', shard_id='shard-0',
            use_key=lambda key, record: seal_entry(key=key, record=record, key_map=key_map, tenant=tenant,
                entry=target, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes))
        writer.attach_user_key(locator=locator, expected_revision=1, user_key_id=user_key.key_id,
            encrypted_dek=user_key.encrypted_dek, scope_purpose='credential', scope_epoch='2026-11',
            scope_key_version='v1', pool_version=user_key.pool_version,
            shard_id=user_key.shard_id, key_arn=user_key.key_arn)
        writer.put_entry(locator=locator, purpose=target.purpose, epoch=target.epoch,
            ciphertext=entry.entry.ciphertext, expected_revision=2, user_key_id=user_key.key_id,
            pool_version=user_key.pool_version, shard_id=user_key.shard_id, key_arn=user_key.key_arn)
        store = GuardedDeletion(auth_table=AUTH, registry_table=REGISTRY, vault_table=VAULT,
                                capsule_table=CAPSULE, approved_purposes=frozenset({"BILLING"}), client=client)
        encrypted = {}
        for request_id in ids:
            encrypted[request_id] = encryptor.seal(request_id, bytearray(internal_id.encode()), bytearray(locator.encode()))
        def call(request_id, vault_revision, capsule_revision=3):
            store.lock_and_register(subject=subject, request_id=request_id,
                snapshot=VaultSnapshot(subject, vault_id, vault_revision, locator, capsule_revision, 0),
                encrypted_user_id=encrypted[request_id]["internal_user_id"],
                encrypted_capsule_locator=encrypted[request_id]["capsule_locator"],
                requested_at="2026-11-21T00:00:00Z", policy_version="synthetic-v1")
        try:
            call(stale_id, 1)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Stale Vault revision was accepted")
        try:
            call(stale_capsule_id, 2, 2)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Stale Capsule revision was accepted")
        after_stale_vault = vault.get_item(Key=vault_key, ConsistentRead=True)["Item"]
        if auth.get_item(Key=auth_key, ConsistentRead=True)["Item"].get("suspended") or \
                after_stale_vault.get("state") != "ACTIVE" or int(after_stale_vault["revision"]) != 2 or \
                capsule.get_item(Key={"pk": locator, "sk": "META"}, ConsistentRead=True)["Item"]["state"] != "ACTIVE" or \
                any(registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{request_id}"},
                                      ConsistentRead=True).get("Item") for request_id in (stale_id, stale_capsule_id)):
            raise AssertionError("Stale transaction left a partial mutation")
        def compete(request_id):
            try:
                call(request_id, 2)
                return request_id, "CREATED"
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                return request_id, "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, (first_id, second_id)))
        if sorted(result for _, result in results) != ["CONFLICT", "CREATED"]:
            raise AssertionError("Competing registrations did not produce exactly one winner")
        winner = next(request_id for request_id, result in results if result == "CREATED")
        call(winner, 2)  # Same client token and exact transaction, within DynamoDB's retry window.
        principal = auth.get_item(Key=auth_key, ConsistentRead=True)["Item"]
        vault_item = vault.get_item(Key=vault_key, ConsistentRead=True)["Item"]
        capsule_item = capsule.get_item(Key={"pk": locator, "sk": "META"}, ConsistentRead=True)["Item"]
        tombstone = registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{winner}"}, ConsistentRead=True).get("Item")
        if not tombstone or principal.get("deletion_lock_request") != winner or int(principal["epoch"]) != 1 or \
                any(item.get("deletion_request_id") != winner or item.get("state") != "DELETION_LOCKED"
                    for item in (vault_item, capsule_item)):
            raise AssertionError("Lock and tombstone state differ")
        if store.recover_locked_request(subject=subject, request_id=winner, vault_id=vault_id, internal_user_id=internal_id,
                                        capsule_locator=locator) != {'status': 'DELETING'}:
            raise AssertionError('guarded recovery did not return safe status')
        try:
            store.recover_locked_request(subject=subject, request_id=str(uuid.uuid4()), vault_id=vault_id, internal_user_id=internal_id,
                                          capsule_locator=locator)
        except ValueError:
            pass
        else:
            raise AssertionError('recovery accepted a different request')
        recovery = GuardedDeletionRecovery(store=store, recovery_cipher=decryptor, client=client)
        if recovery.recover(authenticated_subject=subject, request_id=winner) != {"status": "DELETING"}:
            raise AssertionError("encrypted guarded recovery failed")
        called = []
        def inspect_locked(internal_buffer, locator_buffer):
            if bytes(internal_buffer) != internal_id.encode():
                raise AssertionError('recovered internal ID differs')
            inventory = CapsuleInventory(CAPSULE, purposes, client=client)
            plan = prepare_from_resolved_inventory(inventory=inventory, locator=locator_buffer.decode('ascii'),
                request_id=winner, internal_id=internal_buffer, tenant_resolver=resolver,
                key_map=key_map, approved_purposes=purposes,
                approved_tenant_purposes=tenant_purposes, kms_client=session.client('kms'))
            if plan.entries != (target,):
                raise AssertionError('synthetic deletion target differs')
            called.append('checked')
        if recovery.use_locked(authenticated_subject=subject, request_id=winner, action=inspect_locked) != {'status': 'DELETING'} or called != ['checked']:
            raise AssertionError('locked inventory action failed')
        plan_request = winner
        plan_progress = DeletionProgressStore(auth_table=AUTH, registry_table=REGISTRY,
            progress_table=progress_table.name, client=client)
        plan_cipher = DeletionCheckpointCipher(key_arn, 'dev', source_table_arn=capsule.table_arn,
                                               kms_client=session.client('kms'))
        profile_cipher = DeletionCheckpointCipher(key_arn, 'dev', source_table_arn=vault.table_arn, kms_client=session.client('kms'))
        profile_inventory = ProfileDeletionInventory(source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        profile_worker = ProfileDeletionWorker(inventory=profile_inventory, progress_store=plan_progress, cipher=profile_cipher, source_table=vault)
        profile_residual = ProfileResidualScan(source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        profile_coordinator = ProfileDeletionCoordinator(inventory=profile_inventory, worker=profile_worker, residual=profile_residual)
        profile_recovery = ProfileDeletionRecovery(recovery=recovery, coordinator=profile_coordinator)
        profile_owner = f'SCOPE#USER#{subject}'
        profile_keys = [{'pk':f'{profile_owner}#CONDITION_PROFILE#p','sk':'VERSION#v1'},
                        {'pk':f'{profile_owner}#CONDITION_PROFILES','sk':'PROFILE#p#VERSION#v1'}]
        for profile_key in profile_keys:
            vault.put_item(Item={**profile_key,'profile_id':'p','profile_version':'v1'}, ConditionExpression=Attr('pk').not_exists())
            profile_created.append(profile_key)
        for _ in range(1000):
            profile_result = profile_recovery.step(authenticated_subject=subject,request_id=winner,limit=100)
            if profile_result['residual_found']: raise AssertionError('unexpected profile residual')
            if profile_result['phase']=='VERIFYING': break
        else: raise AssertionError('bounded profile recovery did not finish')
        if any(vault.get_item(Key=k,ConsistentRead=True).get('Item') for k in profile_keys):
            raise AssertionError('synthetic profile was not deleted')
        try:
            profile_recovery.step(authenticated_subject=str(uuid.uuid4()), request_id=winner)
        except ValueError: pass
        else: raise AssertionError('other subject recovered profile deletion')
        job_pk=f'SCOPE#USER#{subject}#JOB#job'
        for job_item in ({'pk':job_pk,'sk':'META','owner_subject':subject,'job_id':'job','version':1},
                         {'pk':job_pk,'sk':'EVENT#1'}):
            vault.put_item(Item=job_item,ConditionExpression=Attr('pk').not_exists())
            job_created.append({'pk':job_item['pk'],'sk':job_item['sk']})
        jobs=JobDeletionInventory(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        job_worker=JobDeletionWorker(inventory=jobs,source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        job_bindings=JobBindingInventory(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        job_residual=JobResidualScan(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        binding_residual=JobBindingResidualScan(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        metadata=JobMetaDeletion(inventory=jobs,residual=job_residual,binding_residual=binding_residual)
        final_jobs=JobFinalResidualScan(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        final_bindings=JobBindingFinalResidualScan(source_table=vault,progress_store=plan_progress,cipher=profile_cipher)
        job_coordinator=JobDeletionCoordinator(jobs=jobs,worker=job_worker,bindings=job_bindings,
            residual=job_residual,binding_residual=binding_residual,metadata=metadata,
            final_jobs=final_jobs,final_bindings=final_bindings,sources=JobSourceSnapshot(inventory=jobs))
        job_recovery=JobDeletionRecovery(recovery=recovery,coordinator=job_coordinator)
        for _ in range(1000):
            job_result=job_recovery.step(authenticated_subject=subject,request_id=winner,limit=100)
            if job_result.get('residual_found'):raise AssertionError('unexpected synthetic job residual')
            if job_result['phase']=='VERIFYING':break
        else:raise AssertionError('bounded job recovery did not finish')
        if any(vault.get_item(Key=key,ConsistentRead=True).get('Item') for key in job_created):
            raise AssertionError('job recovery did not remove META and event')
        snapshot=plan_progress.load_checkpoint(cipher=profile_cipher,subject=subject,request_id=winner,
                                               domain='JOB_META_000000000000')
        if not snapshot:raise AssertionError('job recovery did not preserve encrypted META snapshot')
        try:job_recovery.step(authenticated_subject=str(uuid.uuid4()),request_id=winner)
        except ValueError:pass
        else:raise AssertionError('other subject recovered job deletion')
        meal_id = str(uuid.uuid4())
        meal_pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
        for meal_item in ({'pk': meal_pk, 'sk': 'META', 'meal_batch_id': meal_id, 'version': 1},
                          {'pk': meal_pk, 'sk': 'EVENT#1'},
                          {'pk': f'SCOPE#USER#{subject}#COMBI_MEALS', 'sk': f'MEAL#{meal_id}',
                           'meal_batch_id': meal_id}):
            vault.put_item(Item=meal_item, ConditionExpression=Attr('pk').not_exists())
            meal_created.append({'pk': meal_item['pk'], 'sk': meal_item['sk']})
        meals = MealDeletionInventory(source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        meal_worker = MealDeletionWorker(inventory=meals, source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        meal_residual = MealResidualScan(source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        meal_final = MealFinalResidualScan(source_table=vault, progress_store=plan_progress, cipher=profile_cipher)
        meal_meta = MealMetaDeletion(inventory=meals, residual=meal_residual)
        meal_coordinator = MealDeletionCoordinator(meals=meals, worker=meal_worker, residual=meal_residual,
            metadata=meal_meta, final_residual=meal_final, sources=MealSourceSnapshot(inventory=meals))
        meal_recovery = MealDeletionRecovery(recovery=recovery, coordinator=meal_coordinator)
        try:
            meal_recovery.step(authenticated_subject=str(uuid.uuid4()), request_id=winner)
        except ValueError:
            pass
        else:
            raise AssertionError('other subject recovered meal deletion')
        for _ in range(1000):
            meal_result = meal_recovery.step(authenticated_subject=subject, request_id=winner, limit=100)
            if meal_result.get('residual_found') or meal_result.get('source_ownership_required'):
                raise AssertionError('unexpected synthetic meal residual or source')
            if meal_result['phase'] == 'VERIFYING':
                break
        else:
            raise AssertionError('bounded meal recovery did not finish')
        if any(vault.get_item(Key=k, ConsistentRead=True).get('Item') for k in meal_created):
            raise AssertionError('meal recovery did not remove synthetic records')
        if not plan_progress.load_checkpoint(cipher=profile_cipher, subject=subject, request_id=winner,
                                             domain='MEAL_META_000000000000'):
            raise AssertionError('meal recovery did not preserve encrypted META snapshot')
        cooking = CookingDeletionRecovery(profile=profile_recovery, job=job_recovery, meal=meal_recovery,
                                          progress_store=plan_progress, cipher=profile_cipher)
        if cooking.step(authenticated_subject=subject, request_id=winner, limit=100) != \
                {'phase': 'QUIESCENCE', 'writer_quiescence_required': True}:
            raise AssertionError('cooking recovery failed to require writer quiescence')
        try:
            cooking.step(authenticated_subject=str(uuid.uuid4()), request_id=winner)
        except (ValueError, DeletionProgressConflict):
            pass
        else:
            raise AssertionError('other subject recovered cooking deletion')
        worker = CapsulePlanWorker(recovery=recovery, inventory=CapsuleInventory(CAPSULE, purposes, client=client),
            source_table=capsule, progress_store=plan_progress, progress_cipher=plan_cipher,
            tenant_resolver=resolver, key_map=key_map, approved_purposes=purposes,
            approved_tenant_purposes=tenant_purposes, kms_client=session.client('kms'))
        expected_plan = {'state': 'PREPARED', 'target_count': 1, 'pages': 1}
        if worker.prepare(authenticated_subject=subject, request_id=winner) != expected_plan or \
                worker.prepare(authenticated_subject=subject, request_id=winner) != expected_plan:
            raise AssertionError('durable capsule plan retry failed')
        visited = []
        def check_target(item):
            if item != target:
                raise AssertionError('verified capsule target differs')
            visited.append(True)
        if worker.use_prepared(authenticated_subject=subject, request_id=winner, action=check_target) != \
                {'state': 'VISITED', 'target_count': 1} or visited != [True]:
            raise AssertionError('prepared capsule plan validation failed')
        loser = second_id if winner == first_id else first_id
        if registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{loser}"}, ConsistentRead=True).get("Item"):
            raise AssertionError("Losing request created a tombstone")
    finally:
        if plan_request:
            for profile_key in profile_created + job_created + meal_created:
                vault.delete_item(Key=profile_key)
            cursor = None
            while True:
                query = {'KeyConditionExpression': Key('pk').eq(f'REQ#{plan_request}'), 'ConsistentRead': True}
                if cursor:
                    query['ExclusiveStartKey'] = cursor
                pages = progress_table.query(**query)
                for item in pages['Items']:
                    if item['sk'].startswith(('DOMAIN#PROFILE_', 'DOMAIN#JOB_', 'DOMAIN#MEAL_', 'DOMAIN#COOKING_')):
                        profile_progress_key = {'pk': item['pk'], 'sk': item['sk']}
                        progress_table.delete_item(Key=profile_progress_key)
                        if progress_table.get_item(Key=profile_progress_key, ConsistentRead=True).get('Item'):
                            raise RuntimeError('synthetic profile progress cleanup failed')
                cursor = pages.get('LastEvaluatedKey')
                if not cursor:
                    break
            for profile_key in profile_created + job_created + meal_created:
                if vault.get_item(Key=profile_key, ConsistentRead=True).get('Item'):
                    raise RuntimeError('synthetic profile cleanup failed')
            for domain in ('CAPSULE_PLAN', 'CAPSULE_PLAN_PAGE_000000000001'):
                plan_key = {'pk': f'REQ#{plan_request}', 'sk': f'DOMAIN#{domain}'}
                progress_table.delete_item(Key=plan_key)
                if progress_table.get_item(Key=plan_key, ConsistentRead=True).get('Item'):
                    raise RuntimeError('synthetic plan cleanup failed')
        for request_id in ids:
            key = {"pk": "REGISTRY", "sk": f"REQ#{request_id}"}
            item = registry.get_item(Key=key, ConsistentRead=True).get("Item")
            if item:
                if item.get("request_id") != request_id or item.get("policy_version") != "synthetic-v1":
                    raise RuntimeError("Refusing to delete an unexpected registry item")
                registry.delete_item(Key=key, ConditionExpression=Attr("request_id").eq(request_id) &
                                     Attr("policy_version").eq("synthetic-v1"))
                if registry.get_item(Key=key, ConsistentRead=True).get("Item"):
                    raise RuntimeError("Synthetic registry cleanup failed")
        if capsule_created:
            remove_partition(capsule, locator)
        if vault_created:
            remove_partition(vault, vault_id)
        if auth_created:
            auth.delete_item(Key=auth_key, ConditionExpression=Attr("provenance").eq("SYNTHETIC"))
            if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item"):
                raise RuntimeError("Synthetic auth cleanup failed")
    print(json.dumps({"stale_vault_and_capsule_atomicity": "verified", "competing_registration": "verified",
                      "identical_retry": "verified", "locked_recovery": "verified", "encrypted_recovery": "verified", "locked_encrypted_target_plan": "verified", "durable_plan_retry": "verified", "verified_target_visit": "verified", "profile_recovery": "verified", "job_recovery": "verified", "meal_recovery": "verified", "cooking_recovery": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
