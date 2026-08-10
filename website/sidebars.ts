import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

/**
 * Two sidebars:
 *  - mainSidebar: the presentation content (overview, architecture, deployment, API).
 *  - referenceSidebar: a clean copy of the developer docs, kept secondary.
 */
const sidebars: SidebarsConfig = {
  mainSidebar: [
    {
      type: 'category',
      label: 'Overview',
      collapsed: false,
      items: ['overview/concepts', 'overview/data-flow'],
    },
    {
      type: 'category',
      label: 'Architecture',
      collapsed: false,
      link: {type: 'doc', id: 'architecture/index'},
      items: [
        'architecture/orchestrator',
        'architecture/image-builder',
        'architecture/server',
        'architecture/client',
        'architecture/plugins',
        'architecture/watcher',
        'architecture/rewards-tools',
      ],
    },
    'deployment',
    'api',
  ],

  referenceSidebar: [
    'reference/getting_started',
    'reference/full_flow_example',
    'reference/image_builder',
    'reference/plugins',
    'reference/client',
    'reference/tools',
    'reference/mcp',
    'reference/local_deployment',
    'reference/remote_deployment',
    'reference/database_rollback',
    'reference/http_error_codes',
    {
      type: 'category',
      label: 'Diagrams',
      items: [
        'reference/diagrams/architecture',
        'reference/diagrams/server',
        'reference/diagrams/plugins',
      ],
    },
  ],
};

export default sidebars;
