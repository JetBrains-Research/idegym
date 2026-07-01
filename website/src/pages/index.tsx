import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Mermaid from '@theme/Mermaid';

import styles from './index.module.css';

const HEADLINE_DIAGRAM = `flowchart LR
    user(["👤 You<br/>RL trainer · agent · researcher"]):::client
    orch{{"🎛️ Orchestrator<br/>the control plane"}}:::ctrl
    pods[["📦 Disposable environments<br/>sandboxed pods"]]:::pod

    user -->|"define · build · run · evaluate"| orch
    orch -->|"provision &amp; forward"| pods
    pods -->|"results &amp; rewards"| orch
    orch -.->|"clean up automatically"| pods

    click user "/idegym/architecture/client" "The client & MCP access"
    click orch "/idegym/architecture" "Open the interactive architecture"
    click pods "/idegym/architecture/server" "Inside an environment pod"

    classDef client fill:#1c7ed6,stroke:#1864ab,color:#fff;
    classDef ctrl fill:#e8590c,stroke:#c04405,color:#fff;
    classDef pod fill:#7048e8,stroke:#5f3dc4,color:#fff;`;

function Hero() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero', styles.heroBanner)}>
      <div className="container">
        <p className={styles.heroEyebrow}>Disposable dev environments, at scale</p>
        <h1 className={styles.heroTitle}>{siteConfig.title}</h1>
        <p className={styles.heroSubtitle}>
          Think <strong>GitHub Codespaces for RL training and agent evaluation</strong> —
          but built for <strong>thousands of parallel, short-lived</strong> environments.
        </p>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" to="/architecture">
            Explore the architecture →
          </Link>
          <Link className="button button--secondary button--lg" to="/overview/concepts">
            Start with the concepts
          </Link>
        </div>
      </div>
    </header>
  );
}

function HeadlineDiagram() {
  return (
    <section className={styles.diagramSection}>
      <div className="container">
        <p className={styles.diagramHint}>
          The whole system in one picture — <strong>click a node</strong> to dive in.
        </p>
        <div className={styles.diagramFrame}>
          <Mermaid value={HEADLINE_DIAGRAM} />
        </div>
      </div>
    </section>
  );
}

type Feature = {title: string; body: ReactNode};

const FEATURES: Feature[] = [
  {
    title: '🚀 Scalable orchestration',
    body: 'Spin up and tear down Kubernetes-based environments on demand — thousands of parallel, short-lived sandboxes for training and evaluation.',
  },
  {
    title: '🧩 Plugin-based images',
    body: 'Compose container images from reusable plugins via a Python fluent API or YAML. No hand-written Dockerfiles.',
  },
  {
    title: '🛠️ Tools & rewards',
    body: 'Every environment exposes bash, file edits, and reward signals (compilation, setup, tests) — the primitives an agent needs to act and be scored.',
  },
  {
    title: '🤖 MCP-native',
    body: 'The orchestrator and each server speak MCP. Agents discover and call every operation as a tool, no REST plumbing required.',
  },
  {
    title: '🧹 Self-cleaning',
    body: 'A background watcher reconciles the database against live cluster state, evicting stale servers and reclaiming resources automatically.',
  },
  {
    title: '🔭 Full observability',
    body: 'Built-in Prometheus metrics, Grafana dashboards, and distributed tracing via OpenTelemetry across the orchestrator and every pod.',
  },
];

function Features() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FEATURES.map((f) => (
            <div className={clsx('col col--4', styles.featureCol)} key={f.title}>
              <div className={styles.featureCard}>
                <h3>{f.title}</h3>
                <p>{f.body}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

type Audience = {tag: string; title: string; body: ReactNode; to: string; cta: string};

const AUDIENCES: Audience[] = [
  {
    tag: 'New here?',
    title: 'What IdeGYM is, in plain language',
    body: 'A glossary of the moving parts — clients, orchestrator, server pods, images, plugins, rewards, the watcher — with no Kubernetes knowledge assumed.',
    to: '/overview/concepts',
    cta: 'Read the concepts',
  },
  {
    tag: 'Researchers',
    title: 'The full RL / eval lifecycle',
    body: 'Define an environment → build an image → provision a sandbox → act with tools → score with rewards → clean up. The end-to-end data flow, with diagrams.',
    to: '/overview/data-flow',
    cta: 'Trace the data flow',
  },
  {
    tag: 'Developers',
    title: 'Component deep dives & extension points',
    body: 'Responsibilities, key classes, entry points, and the three plugin hook points — each page links straight into the source on GitHub.',
    to: '/architecture',
    cta: 'Open the architecture',
  },
];

function Audiences() {
  return (
    <section className={styles.audiences}>
      <div className="container">
        <h2 className={styles.sectionHeading}>Written in layers — pick yours</h2>
        <div className="row">
          {AUDIENCES.map((a) => (
            <div className="col col--4" key={a.tag}>
              <div className={styles.audienceCard}>
                <span className={styles.audienceTag}>{a.tag}</span>
                <h3>{a.title}</h3>
                <p>{a.body}</p>
                <Link to={a.to}>{a.cta} →</Link>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={`${siteConfig.title} — disposable dev environments at scale`}
      description="IdeGYM is a framework for creating disposable, scalable development environments for RL training and AI agent evaluation.">
      <Hero />
      <main>
        <HeadlineDiagram />
        <Features />
        <Audiences />
      </main>
    </Layout>
  );
}
