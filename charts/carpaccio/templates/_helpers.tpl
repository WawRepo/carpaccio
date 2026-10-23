{{- define "carpaccio.name" -}}
{{ .Chart.Name }}
{{- end }}

{{/*
The release name alone when it already says "carpaccio", so `helm install
carpaccio` makes objects named carpaccio, not carpaccio-carpaccio. The
homelab moved from plain manifests named carpaccio to this chart, and a
different name would have made new objects beside the live ones.
*/}}
{{- define "carpaccio.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}
{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end -}}
{{- end }}

{{- define "carpaccio.labels" -}}
app.kubernetes.io/name: {{ include "carpaccio.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "carpaccio.selectorLabels" -}}
app.kubernetes.io/name: {{ include "carpaccio.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
