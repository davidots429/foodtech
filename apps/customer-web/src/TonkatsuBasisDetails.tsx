import type { RecipeProposal } from "./recipeResult";

export function TonkatsuBasisDetails({ proposal }: { proposal: RecipeProposal }) {
  const basis = proposal.review_basis;
  if (!basis) return null;
  return <dl>
    <div><dt>기준 레시피 버전</dt><dd>{basis.reference_recipe_version}</dd></div>
    <div><dt>승인된 합성 기준 목표 → 권고</dt>
      <dd>{basis.target_temperature_c}°C → {proposal.data.temperature_c}°C ·
        {" "}{basis.target_duration_seconds}초 → {proposal.data.duration_seconds}초</dd></div>
    <div><dt>적용 검토 범위</dt><dd>두께 {basis.minimum_thickness_cm}cm 이상 {basis.maximum_thickness_cm}cm
      {basis.include_maximum_thickness ? " 이하" : " 미만"} · 관찰한 {basis.observed_batch_mass_kg}kg 배치</dd></div>
    <div><dt>조리량 보정</dt><dd>관찰한 배치의 조건만 제안합니다. 다른 조리량에 대한 보정값은 확인되지 않았습니다.</dd></div>
    <div><dt>권고 제한</dt><dd>{basis.minimum_temperature_c}~{basis.maximum_temperature_c}°C ·
      {" "}{basis.minimum_duration_seconds}~{basis.maximum_duration_seconds}초</dd></div>
    <div><dt>판정 근거</dt><dd>{basis.reason_code === "PRIOR_INITIAL_SAFETY_FAILURE"
      ? `직전 최초 안전 실패를 반영해 실제 조리시간 ${basis.previous_actual_duration_seconds}초보다 짧은 조건을 제외했습니다.`
      : "합성 공정의 온도와 시간 제한을 충족했습니다."}</dd></div>
  </dl>;
}
