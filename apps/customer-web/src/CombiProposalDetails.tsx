import type { CombiProposal } from "./combiRecipeResult";

export function CombiProposalDetails({ proposal, reviewed = false }: { proposal: CombiProposal; reviewed?: boolean }) {
  const d = proposal.data;
  return <div className="combi-proposal"><p>{reviewed ? "승인된 검토에서 보존한 조리조건" : "저장된 권고 · 운영자 검토 필요"}</p>
    <dl><dt>조리 온도</dt><dd>{d.temperature_c} °C</dd>
      <dt>조리 시간</dt><dd>{d.duration_seconds}초</dd>
      <dt>기후 설정</dt><dd>{d.climate_setpoint_percent}% (상대 설정값)</dd>
      <dt>팬 속도</dt><dd>{d.fan_level}단계</dd>
      {proposal.menu === "RICE" ? <><dt>유효 물 비율</dt><dd>{d.effective_water_ratio}</dd>
        <dt>불림 시간</dt><dd>{d.soak_seconds}초</dd><dt>물 빼기 시간</dt><dd>{d.drain_seconds}초</dd></> :
        <><dt>건열 전환 시간</dt><dd>{d.transition_duration_seconds}초</dd></>}
    </dl>
    <table><caption>권장 팬 배치</caption><thead><tr><th>랙</th><th>선반</th></tr></thead>
      <tbody>{d.pan_positions.map(p => <tr key={`${p.rack}-${p.shelf}`}><td>{p.rack}</td><td>{p.shelf}</td></tr>)}</tbody></table>
  </div>;
}
