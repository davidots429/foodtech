const profiles = {
  THIN: { range: ["0.5", "1.0", false], times: [150, 180, 240] },
  STANDARD: { range: ["1.0", "1.5", false], times: [270, 300, 360] },
  THICK: { range: ["1.5", "2.0", true], times: [330, 360, 420] },
} as const;

export type TonkatsuReviewBasis = {
  rule_version: "tonkatsu-approved-targets-v1";
  reference_recipe_version: string;
  profile: keyof typeof profiles;
  target_temperature_c: number;
  target_duration_seconds: number;
  minimum_thickness_cm: string;
  maximum_thickness_cm: string;
  include_maximum_thickness: boolean;
  observed_batch_mass_kg: string;
  load_scope: "OBSERVED_BATCH_ONLY";
  minimum_temperature_c: number;
  maximum_temperature_c: number;
  minimum_duration_seconds: number;
  maximum_duration_seconds: number;
  previous_actual_duration_seconds: number;
  reason_code: "WITHIN_APPROVED_LIMITS" | "PRIOR_INITIAL_SAFETY_FAILURE";
};

export function parseTonkatsuReviewBasis(raw: unknown, data: Record<string, string | number>): TonkatsuReviewBasis {
  const fail = () => { throw new Error("돈까스 권고의 기준 조리조건을 확인하지 못했습니다."); };
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return fail();
  const value = raw as Record<string, unknown>;
  const profile = value.profile;
  if (profile !== "THIN" && profile !== "STANDARD" && profile !== "THICK") return fail();
  const reference = value.reference_recipe_version;
  const mass = value.observed_batch_mass_kg;
  const prior = value.previous_actual_duration_seconds;
  const reason = value.reason_code;
  if (typeof reference !== "string" || !reference.trim() || reference.length > 80 ||
      typeof mass !== "string" || !/^[0-2](?:\.\d{0,9}[1-9])?$/.test(mass) || mass.length > 12 ||
      Number(mass) < 0.001 || Number(mass) > 2.5 ||
      typeof prior !== "number" || !Number.isSafeInteger(prior) || prior < 1 || prior > 86400 ||
      (reason !== "WITHIN_APPROVED_LIMITS" && reason !== "PRIOR_INITIAL_SAFETY_FAILURE")) return fail();

  const { range: [low, high, closed], times: [minimum, target, maximum] } = profiles[profile];
  const expected: TonkatsuReviewBasis = {
    rule_version: "tonkatsu-approved-targets-v1", reference_recipe_version: reference, profile,
    target_temperature_c: 170, target_duration_seconds: target,
    minimum_thickness_cm: low, maximum_thickness_cm: high, include_maximum_thickness: closed,
    observed_batch_mass_kg: mass, load_scope: "OBSERVED_BATCH_ONLY",
    minimum_temperature_c: 165, maximum_temperature_c: 175,
    minimum_duration_seconds: reason === "PRIOR_INITIAL_SAFETY_FAILURE" ? Math.max(minimum, prior) : minimum,
    maximum_duration_seconds: maximum, previous_actual_duration_seconds: prior, reason_code: reason,
  };
  if (Object.keys(value).length !== Object.keys(expected).length ||
      Object.entries(expected).some(([name, field]) => value[name] !== field) ||
      Number(data.temperature_c) < 165 || Number(data.temperature_c) > 175 ||
      typeof data.duration_seconds !== "number" || data.duration_seconds < expected.minimum_duration_seconds ||
      data.duration_seconds > maximum) return fail();
  return expected;
}
