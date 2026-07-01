import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const GITHUB_REPO = 'https://github.com/JetBrains-Research/idegym';

const config: Config = {
  title: 'IdeGYM',
  tagline: 'Disposable, scalable dev environments for RL training and agent evaluation at scale',
  favicon: 'img/idegym-logo.png',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Production URL + base path for GitHub Pages (jetbrains-research.github.io/idegym/).
  url: 'https://jetbrains-research.github.io',
  baseUrl: '/idegym/',

  // GitHub Pages deployment config.
  organizationName: 'JetBrains-Research',
  projectName: 'idegym',
  trailingSlash: false,

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  // Native Mermaid support with clickable nodes.
  // `format: detect` parses .md as CommonMark (so the copied reference docs
  // are robust against MDX/JSX pitfalls) and .mdx as MDX.
  markdown: {
    mermaid: true,
    format: 'detect',
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          // Serve docs at the site root (e.g. /architecture, /overview/concepts).
          routeBasePath: '/',
          editUrl: `${GITHUB_REPO}/tree/main/website/`,
        },
        // Presentation site — no blog.
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
    [
      'redocusaurus',
      {
        specs: [
          {
            id: 'orchestrator',
            spec: 'static/openapi/orchestrator.json',
            route: '/api/orchestrator',
          },
          {
            id: 'server',
            spec: 'static/openapi/server.json',
            route: '/api/server',
          },
        ],
        theme: {
          primaryColor: '#f74b00',
        },
      },
    ],
  ],

  themeConfig: {
    image: 'img/idegym-logo.png',
    // Dark mode first — the site is built to present on a projector.
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    docs: {
      sidebar: {
        hideable: true,
      },
    },
    // Mermaid light/dark palettes, legible on a projector.
    // securityLevel: 'loose' is required for `click NodeId "url"` drill-down
    // links to navigate.
    // Adaptive light/dark chrome (edges, cluster labels stay readable per mode);
    // vivid per-node colors come from `classDef` in each diagram and render the
    // same in both modes. securityLevel 'loose' enables `click` drill-down links.
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
      options: {
        securityLevel: 'loose',
        flowchart: {
          useMaxWidth: true,
          htmlLabels: true,
          curve: 'basis',
          nodeSpacing: 45,
          rankSpacing: 60,
          padding: 12,
        },
        sequence: {useMaxWidth: true, mirrorActors: false, messageAlign: 'center'},
      },
    },
    navbar: {
      title: 'IdeGYM',
      logo: {
        alt: 'IdeGYM',
        src: 'img/idegym-logo.png',
      },
      items: [
        {
          type: 'dropdown',
          label: 'Overview',
          position: 'left',
          items: [
            {to: '/overview/concepts', label: 'Core concepts'},
            {to: '/overview/data-flow', label: 'Data & usage flow'},
          ],
        },
        {to: '/architecture', label: 'Architecture', position: 'left'},
        {to: '/deployment', label: 'Deployment', position: 'left'},
        {to: '/api', label: 'API', position: 'left'},
        {
          type: 'docSidebar',
          sidebarId: 'referenceSidebar',
          position: 'left',
          label: 'Reference',
        },
        {
          href: GITHUB_REPO,
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Start here',
          items: [
            {label: 'What is IdeGYM?', to: '/'},
            {label: 'Core concepts', to: '/overview/concepts'},
            {label: 'Data & usage flow', to: '/overview/data-flow'},
          ],
        },
        {
          title: 'Architecture',
          items: [
            {label: 'Interactive diagram', to: '/architecture'},
            {label: 'Orchestrator', to: '/architecture/orchestrator'},
            {label: 'Image builder', to: '/architecture/image-builder'},
            {label: 'Plugins', to: '/architecture/plugins'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Deployment', to: '/deployment'},
            {label: 'API', to: '/api'},
            {label: 'Reference docs', to: '/reference/getting_started'},
            {label: 'GitHub', href: GITHUB_REPO},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} JetBrains. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'yaml', 'json', 'docker', 'toml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
