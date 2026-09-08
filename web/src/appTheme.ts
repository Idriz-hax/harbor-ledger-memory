import { createTheme } from '@mui/material/styles'

export const DEFAULT_PRESET = 'dusk'

/* Activity colour palette — maps event types to glowing node colours. */
export const activityColors = {
  read: '#59d9b1',
  write: '#ff8a70',
  scan: '#6d9cff',
  propose: '#bd7cff',
  default: '#546e7a',
} as const

export type ActivityKind = keyof typeof activityColors

/* Single dark nautical theme. The old multi-preset system is retired;
   all chart tones resolve to this one deep-sea palette. */
export const chartPresets = {
  dusk: {
    mode: 'dark',
    background: '#0b1117',
    water: '#0c1117',
    grid: 'rgba(118, 163, 174, .14)',
    major: 'rgba(118, 163, 174, .26)',
    paper: '#121a22',
    ink: '#e9f3f2',
    text: '#e9f3f2',
    muted: '#879ba3',
    route: '#546e7a',
    accent: '#e6bf69',
    index: '#e6bf69',
  },
} as const

export type ChartPreset = keyof typeof chartPresets

export function presetFor(_value: string | undefined | null): ChartPreset {
  return DEFAULT_PRESET
}

export function makeAppTheme(_preset: ChartPreset | (string & {})) {
  const c = chartPresets.dusk
  return createTheme({
    palette: {
      mode: 'dark',
      primary: { main: c.accent },
      secondary: { main: c.accent },
      background: { default: c.background, paper: c.paper },
      text: {
        primary: c.text,
        secondary: c.muted,
      },
      error: { main: '#f08072' },
      warning: { main: c.accent },
      success: { main: activityColors.read },
      divider: 'rgba(118, 163, 174, .20)',
    },
    typography: {
      fontFamily: '"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif',
      h1: { fontFamily: '"Newsreader", Georgia, "Times New Roman", serif', fontWeight: 600 },
      h2: { fontFamily: '"Newsreader", Georgia, "Times New Roman", serif', fontWeight: 600 },
      h3: { fontFamily: '"Newsreader", Georgia, "Times New Roman", serif', fontWeight: 600 },
      overline: {
        fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
        fontSize: '0.7rem',
        letterSpacing: '0.14em',
      },
    },
    shape: { borderRadius: 8 },
    components: {
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: { root: { textTransform: 'none' } },
      },
      MuiPaper: {
        styleOverrides: {
          root: {
            backgroundImage: 'none',
          },
        },
      },
      MuiListItemButton: {
        styleOverrides: {
          root: {
            borderRadius: 6,
            '&.Mui-selected': {
              background: 'rgba(230, 191, 105, .10)',
              '&:hover': { background: 'rgba(230, 191, 105, .14)' },
            },
            '&:hover': { background: 'rgba(118, 163, 174, .08)' },
          },
        },
      },
    },
  })
}
