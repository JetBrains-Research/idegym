{{/*
Expand the name of the chart.
*/}}
{{- define "idegym.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "idegym.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "idegym.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "idegym.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Name of the deployment service account
*/}}
{{- define "idegym.deployment.serviceAccountName" -}}
{{- if .Values.deployment.serviceAccount.create }}
{{- default (include "idegym.fullname" .) .Values.deployment.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.deployment.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the pod snapshot service account
*/}}
{{- define "idegym.podSnapshot.serviceAccountName" -}}
{{- if .Values.podSnapshot.serviceAccount.create }}
{{- default (printf "%s-snapshot" (include "idegym.fullname" .)) .Values.podSnapshot.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.podSnapshot.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "idegym.selectorLabels" -}}
app: {{ include "idegym.name" . }}
app.kubernetes.io/name: {{ include "idegym.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Render an env var source: primitives become `value: ...` while maps are emitted verbatim
*/}}
{{- define "idegym.envSource" -}}
{{- if kindIs "map" . -}}
{{- toYaml . -}}
{{- else -}}
value: {{ . | quote }}
{{- end -}}
{{- end -}}

{{/*
Fully qualified name of the watcher service
*/}}
{{- define "idegym.watcher.fullname" -}}
{{- printf "%s-watcher" (include "idegym.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Watcher selector labels — distinct from the orchestrator's so its Service never matches watcher pods
*/}}
{{- define "idegym.watcher.selectorLabels" -}}
app: {{ include "idegym.name" . }}-watcher
app.kubernetes.io/name: {{ include "idegym.name" . }}-watcher
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Watcher common labels
*/}}
{{- define "idegym.watcher.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "idegym.watcher.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Name of the watcher service account
*/}}
{{- define "idegym.watcher.serviceAccountName" -}}
{{- if .Values.watcher.serviceAccount.create }}
{{- default (include "idegym.watcher.fullname" .) .Values.watcher.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.watcher.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database environment variables shared by the orchestrator and watcher deployments.
Written at base indentation; include with `nindent 12` into a container env list, e.g.
`{{- include "idegym.databaseEnv" . | nindent 12 }}`.
*/}}
{{- define "idegym.databaseEnv" -}}
- name: IDEGYM_DATABASE_DB
  {{- if .Values.database.name }}
  {{- include "idegym.envSource" .Values.database.name | nindent 2 }}
  {{- else if .Values.postgresql.enabled }}
  value: {{ include "idegym.subchart.postgresql.v1.database" . | quote }}
  {{- else }}
  {{- required "Database name must be specified!" .Values.database.name }}
  {{- end }}
- name: IDEGYM_DATABASE_HOST
  {{- if .Values.database.host }}
  {{- include "idegym.envSource" .Values.database.host | nindent 2 }}
  {{- else if .Values.postgresql.enabled }}
  value: {{ include "idegym.subchart.postgresql.v1.primary.fullname" . | quote }}
  {{- else }}
  {{- required "Database host must be specified!" .Values.database.host }}
  {{- end }}
- name: IDEGYM_DATABASE_PORT
  {{- if .Values.database.port }}
  {{- include "idegym.envSource" .Values.database.port | nindent 2 }}
  {{- else if .Values.postgresql.enabled }}
  value: {{ include "idegym.subchart.postgresql.v1.service.port" . | quote }}
  {{- else }}
  {{- required "Database port must be specified!" .Values.database.port }}
  {{- end }}
- name: IDEGYM_DATABASE_USER
  {{- if .Values.database.username }}
  {{- include "idegym.envSource" .Values.database.username | nindent 2 }}
  {{- else if .Values.postgresql.enabled }}
  value: {{ include "idegym.subchart.postgresql.v1.username" . | quote }}
  {{- else }}
  {{- required "Database username must be specified!" .Values.database.username }}
  {{- end }}
- name: IDEGYM_DATABASE_PASSWORD
  {{- if .Values.database.password }}
  {{- include "idegym.envSource" .Values.database.password | nindent 2 }}
  {{- else if .Values.postgresql.enabled }}
  valueFrom:
    secretKeyRef:
      name: {{ include "idegym.subchart.postgresql.v1.secretName" . | quote }}
      key: {{ include "idegym.subchart.postgresql.v1.userPasswordKey" . | quote }}
  {{- else }}
  {{- required "Database password must be specified!" .Values.database.password }}
  {{- end }}
{{- end -}}

{{/*
Helpers from subcharts
*/}}
{{- define "idegym.subchart.grafana.fullname" -}}
{{- include "grafana.fullname" .Subcharts.grafana -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.database" -}}
{{- include "postgresql.v1.database" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.primary.fullname" -}}
{{- include "postgresql.v1.primary.fullname" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.secretName" -}}
{{- include "postgresql.v1.secretName" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.service.port" -}}
{{- include "postgresql.v1.service.port" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.userPasswordKey" -}}
{{- include "postgresql.v1.userPasswordKey" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.postgresql.v1.username" -}}
{{- include "postgresql.v1.username" .Subcharts.postgresql -}}
{{- end -}}
{{- define "idegym.subchart.prometheus.server.fullname" -}}
{{- include "prometheus.server.fullname" .Subcharts.prometheus -}}
{{- end -}}
{{- define "idegym.subchart.tempo.fullname" -}}
{{- include "tempo.fullname" .Subcharts.tempo -}}
{{- end -}}
