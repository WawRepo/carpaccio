{{- define "carpaccio.name" -}}
{{ .Chart.Name }}
{{- end }}

{{- define "carpaccio.fullname" -}}
{{ .Release.Name }}-{{ .Chart.Name }}
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
