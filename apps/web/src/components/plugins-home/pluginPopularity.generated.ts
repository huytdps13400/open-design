// AUTO-GENERATED — DO NOT EDIT BY HAND.
//
// Blended template popularity, used to order the plugin/example grid and the
// Home rail so the templates users actually reach for lead each category and
// sub-category (OPEND-449). Higher score = more popular; range [0, 1].
//
// How it is built (deterministic, creds-free transform):
//   score = 0.6 * norm(log1p(distinctUsers)) + 0.4 * norm(log1p(runs))
//   • window: trailing 28 days of `run_finished` events (by plugin_id)
//   • distinct users are the anti-gaming signal; runs add engagement depth
//   • log1p tames the head-template scale gap; min-max normalized over the
//     live-catalog template set so both metrics land in [0, 1]
//   • RETIRED plugins (absent from the live catalog) are dropped
//   • templates with no renderable preview are EXCLUDED — mode-seed entries
//     (e.g. the generic Live Artifact / HyperFrames options) live in the
//     composer mode picker, not the gallery, so usage must not float them up
//   • templates below 20 distinct users are OMITTED so thin-sample
//     tail templates keep their curated/visual fallback order
//
// Regenerate with: pnpm exec tsx scripts/refresh-plugin-popularity.ts --write
// Refreshed weekly by .github/workflows/refresh-plugin-popularity.yml.
// See pluginPopularity.RUNBOOK.md here.

export interface PluginPopularityMeta {
  readonly generatedAt: string;
  readonly windowDays: number;
  readonly weights: { readonly users: number; readonly runs: number };
  readonly minUsers: number;
  readonly count: number;
}

export const PLUGIN_POPULARITY_META: PluginPopularityMeta = {
  generatedAt: '2026-08-17',
  windowDays: 28,
  weights: { users: 0.6, runs: 0.4 },
  minUsers: 20,
  count: 89,
};

// Plugin id -> blended popularity score in [0, 1], most-popular first.
export const PLUGIN_POPULARITY: Readonly<Record<string, number>> = {
  'example-web-prototype': 1.0,
  'example-simple-deck': 0.8696,
  'example-web-clone': 0.8522,
  'example-mobile-app': 0.7114,
  'example-open-design-landing': 0.6727,
  'example-webgl-experience': 0.6329,
  'example-gamified-app': 0.611,
  'example-wireframe-mobile-flow': 0.6037,
  'example-fs-creative-voltage': 0.5676,
  'example-kanban-board': 0.5664,
  'example-wireframe-sketch': 0.554,
  'image-template-anime-martial-arts-battle-illustration': 0.5468,
  'example-fs-electric-studio': 0.5406,
  'example-fs-notebook-tabs': 0.5367,
  'example-digital-eguide': 0.5337,
  'example-mobile-onboarding': 0.5336,
  'example-guizang-ppt': 0.5298,
  'example-webgl-caustic-pool': 0.5114,
  'example-social-carousel': 0.5098,
  'example-dashboard': 0.5052,
  'video-template-video-seedance-three-kingdoms-lyubu-yuanmen-archery': 0.4927,
  'example-motion-frames': 0.4868,
  'example-huashu-bento-insight': 0.4853,
  'example-video-hyperframes': 0.4844,
  'example-wireframe-greybox': 0.4792,
  'video-template-seedance-2-0-15-second-cinematic-japanese-romance-short-film': 0.4791,
  'example-social-media-matrix-tracker-template': 0.4776,
  'example-resume-modern': 0.4772,
  'example-fs-editorial-forest': 0.4745,
  'image-template-e-commerce-live-stream-ui-mockup': 0.4735,
  'example-html-ppt-zhangzara-creative-mode': 0.4719,
  'example-huashu-keynote-black': 0.464,
  'example-html-ppt-course-module': 0.4519,
  'image-template-profile-avatar-anime-girl-to-cinematic-photo': 0.4432,
  'example-wireframe-annotated': 0.4416,
  'image-template-profile-avatar-casual-fashion-grid-photoshoot': 0.4382,
  'example-velar-luxury-real-estate': 0.433,
  'example-hps-academic-paper': 0.4303,
  'video-template-frame-kinetic-type': 0.4298,
  'example-huashu-slides': 0.4221,
  'example-codex-interactive-capability-map': 0.4216,
  'example-mockup-device-3d': 0.4214,
  'example-html-ppt-zhangzara-capsule': 0.418,
  'example-blog-post': 0.4165,
  'example-html-ppt-knowledge-arch-blueprint': 0.4154,
  'example-image-poster': 0.4152,
  'example-audio-jingle': 0.4085,
  'example-trading-analysis-dashboard-template': 0.4067,
  'video-template-luxury-supercar-cinematic-narrative': 0.4026,
  'image-template-3d-stone-staircase-evolution-infographic': 0.4005,
  'image-template-illustration-crayon-kid-drawing-rework': 0.395,
  'example-html-ppt-hermes-cyber-terminal': 0.3925,
  'example-huashu-golden-circle': 0.3875,
  'example-pm-spec': 0.3868,
  'example-doc-kami-parchment': 0.3857,
  'example-hps-true-blueprint': 0.3842,
  'video-template-frame-logo-outro': 0.3828,
  'example-html-ppt-zhangzara-scatterbrain': 0.381,
  'image-template-illustrated-city-food-map': 0.38,
  'image-template-notion-team-dashboard-live-artifact': 0.3786,
  'example-webgl-aurora-veil': 0.3769,
  'image-template-momotaro-explainer-slide-in-hybrid-style': 0.3731,
  'video-template-frame-build-minimal': 0.3716,
  'example-html-ppt-zhangzara-block-frame': 0.3691,
  'video-template-3d-animated-boy-building-lego': 0.3688,
  'example-deck-swiss-international': 0.3678,
  'example-kami-deck': 0.3674,
  'image-template-game-screenshot-anime-fighting-game-captain-ryuuga-vs-kaze-renshin': 0.3649,
  'example-critique': 0.3642,
  'example-hps-bauhaus': 0.3639,
  'video-template-frame-bold-poster': 0.3576,
  'example-html-ppt-zhangzara-studio': 0.3575,
  'example-flowai-live-dashboard-template': 0.3571,
  'example-live-dashboard': 0.3537,
  'example-fs-emerald-editorial': 0.352,
  'example-open-design-landing-deck': 0.3515,
  'example-huashu-takram-soft-tech': 0.3492,
  'example-frontend-slides': 0.3475,
  'example-docs-page': 0.3462,
  'example-frame-flowchart-sticky': 0.3439,
  'example-html-ppt-zhangzara-sakura-chroma': 0.3437,
  'example-finance-report': 0.3427,
  'example-webgl-distortion-grain': 0.3397,
  'video-template-frame-glitch-title': 0.3346,
  'video-template-frame-liquid-bg-hero': 0.3309,
  'image-template-infographic-otaku-dance-choreography-breakdown-gokurakujodo-16-panels': 0.3237,
  'example-frame-logo-outro': 0.3213,
  'image-template-profile-avatar-cinematic-south-asian-male-portrait-with-vultures': 0.3213,
  'example-huashu-pentagram-grid': 0.3185,
};

// Templates with no renderable preview — suppressed from the visual gallery
// grid so they never show as an empty letter card. They still reach users
// through the composer's mode picker. Repo-derived (baked manifest + on-disk
// `od.preview` entry existence), refreshed alongside the scores above.
export const PLUGIN_NO_PREVIEW: readonly string[] = [
  'example-dcf-valuation',
  'example-design-brief',
  'example-hatch-pet',
  'example-html-ppt',
  'example-hyperframes',
  'example-last30days',
  'example-live-artifact',
  'example-pptx-html-fidelity-audit',
  'example-x-research',
];
