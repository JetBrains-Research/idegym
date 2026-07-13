import type {ReactNode, CSSProperties} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Mermaid from '@theme/Mermaid';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

import styles from './index.module.css';

const TRY_NOW = 'https://jb.gg/idegym';
const ISSUES = 'https://github.com/JetBrains-Research/idegym/issues';

const HEADLINE_DIAGRAM = `flowchart LR
    user(["<b>👤 You</b><br/>trainer · agent"]):::client
    orch{{"<b>🎛️ Orchestrator</b><br/>control plane"}}:::ctrl
    pods[["<b>📦 Disposable<br/>environments</b>"]]:::pod

    user -->|"define · build · run · evaluate"| orch
    orch -->|"provision & forward"| pods
    pods -->|"results & rewards"| orch
    orch -.->|"clean up"| pods

    click user "/idegym/architecture/client" "Dive into the client library and how agents connect over MCP."
    click orch "/idegym/architecture" "Open the full interactive architecture diagram."
    click pods "/idegym/architecture/server" "Look inside a running, sandboxed environment pod."

    classDef client fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef ctrl fill:#6b57ff,stroke:#5b4bd2,color:#fff;
    classDef pod fill:#4f46e5,stroke:#4338ca,color:#fff;`;

function Hero() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero', styles.heroBanner)}>
      <div className="container">
        <img className={styles.heroLogo} src={useBaseUrl('/img/idegym-logo.png')} alt="IdeGYM" />
        <p className={styles.heroEyebrow}>Disposable dev environments, at scale</p>
        <h1 className={styles.heroTitle}>{siteConfig.title}</h1>
        <p className={styles.heroSubtitle}>
          An open-source orchestrator for scalable, disposable development environments —
          built for <strong>training reinforcement-learning models</strong> and{' '}
          <strong>running AI agents</strong>.
        </p>
        <div className={styles.heroBadge}>Already used by JetBrains Research</div>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" href={TRY_NOW}>
            Try it now →
          </Link>
          <Link className="button button--secondary button--lg" to="/architecture">
            Explore the architecture
          </Link>
          <Link className="button button--secondary button--lg" to="/overview/concepts">
            Core concepts
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

type Problem = {title: string; body: string; accent: string};

const PROBLEMS: Problem[] = [
  {
    title: 'Scale',
    body: 'A single machine can’t run hundreds of parallel environments that reset in seconds. You need a distributed system built for that from the ground up.',
    accent: '#2f6fed',
  },
  {
    title: 'Isolation',
    body: 'Agents must not interfere with each other. Every environment needs its own filesystem, process tree, and network to fully reset between episodes.',
    accent: '#6b57ff',
  },
  {
    title: 'Latency',
    body: 'Spinning up a new container for every cycle is too slow at scale. Environments need to restart or reset in under a second to keep throughput high.',
    accent: '#f45c4a',
  },
];

function Problem() {
  return (
    <section className={styles.sectionAlt}>
      <div className="container">
        <p className={styles.sectionEyebrow}>The problem</p>
        <h2 className={styles.sectionHeading}>Why RL on coding tasks is hard</h2>
        <p className={styles.sectionLead}>
          Training LLMs with reinforcement learning on coding tasks means running tens or
          hundreds of thousands of generation-reward cycles — each needing a clean, isolated
          environment with the right source code and tools.
        </p>
        <div className="row">
          {PROBLEMS.map((p) => (
            <div className="col col--4" key={p.title} style={{marginBottom: '1rem'}}>
              <div className={styles.problemCard} style={{['--accent' as string]: p.accent} as CSSProperties}>
                <h3>{p.title}</h3>
                <p>{p.body}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

type Card = {title: string; body: ReactNode};

const DOES: Card[] = [
  {
    title: '🧪 Disposable environments',
    body: 'Spin up isolated, sandboxed environments on demand and tear them down when finished — no manual cleanup.',
  },
  {
    title: '🧱 Any project, any image',
    body: 'Load projects from a Git URL, archive, or mounted volume, and build custom Docker images via a plugin API.',
  },
  {
    title: '🔀 Request forwarding',
    body: 'Proxy requests from your training loop straight to running pods and return responses, so you can compute rewards offline or replay episodes.',
  },
];

function WhatItDoes() {
  return (
    <section className={styles.section}>
      <div className="container">
        <p className={styles.sectionEyebrow}>What IdeGYM does</p>
        <h2 className={styles.sectionHeading}>A Kubernetes-native environment fleet</h2>
        <p className={styles.sectionLead}>
          IdeGYM manages the full lifecycle of development environments — from image build to
          teardown — on Kubernetes.
        </p>
        <div className="row">
          {DOES.map((f) => (
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

type Mini = {title: string; body: string};

const DEV_FEATURES: Mini[] = [
  {
    title: '🖥️ IDE integration',
    body: 'JetBrains IDEs run headlessly inside pods and route their MCP server through IdeGYM — inspections, refactoring, and code intelligence for any MCP agent, plus a direct HTTP inspection endpoint.',
  },
  {
    title: '🔌 MCP support',
    body: 'Tools, rewards, filesystem, and server lifecycle are exposed as MCP endpoints, so any MCP-compatible agent or framework can use IdeGYM without custom client code.',
  },
  {
    title: '🔗 Integrations',
    body: 'Drop-in support for OpenEnv and verl — works with the RL frameworks you already use.',
  },
  {
    title: '🔄 Episode lifecycle',
    body: 'Reuse environments across episodes: reset just the project folder, restore from a filesystem + memory checkpoint, or restart completely.',
  },
  {
    title: '🐚 Shell execution',
    body: 'Bash execution, file editing, and a filesystem API are available on every pod.',
  },
  {
    title: '🎯 Rewards',
    body: 'Define your own reward signals — run tests, check compilation, validate setup — and plug them directly into your training loop.',
  },
  {
    title: '🧱 Image builder',
    body: 'Use any Docker image as a base. Compose custom images from reusable plugins, in Python or YAML.',
  },
];

const OPS_FEATURES: Mini[] = [
  {
    title: '🚀 One-click deployment',
    body: 'Deploy with a Helm chart, or customize the manifests.',
  },
  {
    title: '🛡️ Resource limits & admission control',
    body: 'Regex-based quota rules per tenant, enforced automatically at admission time.',
  },
  {
    title: '🔭 Observability',
    body: 'Prometheus metrics, OpenTelemetry tracing, and a live dashboard with OOMKill detection.',
  },
  {
    title: '🧹 Automatic cleanup & fault tolerance',
    body: 'A background watcher reconciles database state against live Kubernetes, removing stale resources and recovering crashed or missing pods.',
  },
  {
    title: '🗄️ Audit log',
    body: 'Every forwarded request and response is stored in PostgreSQL — enabling reproducible reward computation and episode replay.',
  },
];

function miniGrid(items: Mini[]) {
  return (
    <div className="row">
      {items.map((f) => (
        <div className="col col--4" key={f.title} style={{marginBottom: '1rem'}}>
          <div className={styles.miniCard}>
            <h4>{f.title}</h4>
            <p>{f.body}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function Features() {
  return (
    <section className={styles.sectionAlt}>
      <div className="container">
        <p className={styles.sectionEyebrow}>Features</p>
        <h2 className={styles.sectionHeading}>Everything a training loop needs</h2>
        <Tabs>
          <TabItem value="dev" label="For researchers & developers" default>
            {miniGrid(DEV_FEATURES)}
          </TabItem>
          <TabItem value="ops" label="For ops">
            {miniGrid(OPS_FEATURES)}
          </TabItem>
        </Tabs>
      </div>
    </section>
  );
}

function Integrations() {
  return (
    <section className={styles.section}>
      <div className="container">
        <p className={styles.sectionEyebrow}>Integrations</p>
        <h2 className={styles.sectionHeading}>Works with your stack</h2>
        <div className="row">
          <div className="col col--6" style={{marginBottom: '1rem'}}>
            <div className={styles.integrationCard}>
              <img
                className={styles.integrationLogo}
                src={useBaseUrl('/img/openenv-pytorch.svg')}
                alt="OpenEnv"
              />
              <h3>OpenEnv</h3>
              <p>
                IdeGYM is WebSocket-compatible with OpenEnv — run OpenEnv environments directly
                on IdeGYM’s Kubernetes infrastructure without changing the agent interface.
              </p>
            </div>
          </div>
          <div className="col col--6" style={{marginBottom: '1rem'}}>
            <div className={styles.integrationCard}>
              <img
                className={styles.integrationLogo}
                src={useBaseUrl('/img/verl.png')}
                alt="verl"
              />
              <h3>verl</h3>
              <p>
                IdeGYM pairs with verl as the environment layer: verl handles RL training,
                IdeGYM provides the scalable sandbox fleet.
              </p>
            </div>
          </div>
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
    body: 'Define an environment → build → provision → act with tools → score with rewards → clean up. The end-to-end data flow, with diagrams.',
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
        <h2 className={styles.sectionHeading}>Dive deeper — pick your layer</h2>
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

function FinalCta() {
  return (
    <section className={styles.ctaBand}>
      <div className="container">
        <h2>Spin up, train, and repeat with IdeGYM</h2>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" href={TRY_NOW}>
            Try it now →
          </Link>
          <Link className="button button--secondary button--lg" href={TRY_NOW}>
            Contribute
          </Link>
          <Link className="button button--secondary button--lg" href={ISSUES}>
            Request a feature
          </Link>
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
      description="IdeGYM is an open-source orchestrator for scalable, disposable development environments for RL training and AI agent evaluation.">
      <Hero />
      <main>
        <HeadlineDiagram />
        <Problem />
        <WhatItDoes />
        <Features />
        <Integrations />
        <Audiences />
        <FinalCta />
      </main>
    </Layout>
  );
}
