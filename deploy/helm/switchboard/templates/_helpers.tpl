{{/*
Names, labels and the few values that more than one template needs (docs/DEPLOY.md, "Helm").
*/}}

{{- define "switchboard.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
The StatefulSet's name, and so its pod's (<name>-0), its claim's and its Service's. A release called
"switchboard" gives "switchboard", as deploy/kubernetes/ names them. 52 characters, because a
StatefulSet's name plus its revision hash must fit a label's 63.
*/}}
{{- define "switchboard.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 52 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "switchboard.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
The selector is name, instance and component. The component is "broker" now so that a later part
(a second kind of pod beside it) can be told apart; a StatefulSet's selector can't change once it
exists, so it is chosen here.
*/}}
{{- define "switchboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "switchboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: broker
{{- end }}

{{- define "switchboard.labels" -}}
helm.sh/chart: {{ include "switchboard.chart" . }}
{{ include "switchboard.selectorLabels" . }}
app.kubernetes.io/version: {{ include "switchboard.imageTag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* A release, never :latest: the chart's appVersion unless image.tag says otherwise. */}}
{{- define "switchboard.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{/*
publicUrl, or a message that says what to set. The broker refuses to start without one, and refuses a
path in it, so both are caught here, where the fix is a values file and not a crash-looping pod.
*/}}
{{- define "switchboard.publicUrl" -}}
{{- $url := required "publicUrl is required: the https:// address browsers use, for example --set publicUrl=https://sb.example.com (the broker answers to no other host)" .Values.publicUrl -}}
{{- if not (regexMatch "^https?://[^/?#@[:space:]]+$" $url) -}}
{{- fail (printf "publicUrl must be an origin, https://<host>[:<port>] with no path, user name or trailing slash; got %q" $url) -}}
{{- end -}}
{{- $url -}}
{{- end }}

{{/*
joinly's memory, in Mi: 2.7 GiB a live call (measured, #604) plus 0.5 GiB, rounded up to a
multiple of 0.5 GiB. Whole numbers only, in tenths of a Mi: 2.7 GiB is 27648 of them. One call is
3584 (3.5 GiB), three are 9216 (9 GiB). The limit is 512 above (see the StatefulSet).
*/}}
{{- define "switchboard.joinlyMemoryMi" -}}
{{- $tenths := add (mul 27648 (int .Values.meetings.maxCalls)) 5120 -}}
{{- mul (div (add $tenths 5119) 5120) 512 -}}
{{- end }}
