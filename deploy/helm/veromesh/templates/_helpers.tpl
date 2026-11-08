{{/* Names */}}
{{- define "veromesh.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "veromesh.fullname" -}}
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

{{- define "veromesh.apiName" -}}
{{- printf "%s-api" (include "veromesh.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "veromesh.fusekiName" -}}
{{- printf "%s-fuseki" (include "veromesh.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "veromesh.fusekiReplicaName" -}}
{{- printf "%s-fuseki-replica" (include "veromesh.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Service in front of the read replicas */}}
{{- define "veromesh.fusekiReplicasService" -}}
{{- printf "%s-fuseki-replicas" (include "veromesh.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "veromesh.replicated" -}}
{{- if and .Values.fuseki.enabled .Values.fuseki.replication.enabled }}true{{ end }}
{{- end }}

{{- define "veromesh.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "veromesh.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Labels */}}
{{- define "veromesh.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "veromesh.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: veromesh
{{- end }}

{{/* Selector labels for one component: include "veromesh.selectorLabels" (list . "api") */}}
{{- define "veromesh.selectorLabels" -}}
{{- $ctx := index . 0 -}}
app.kubernetes.io/name: {{ include "veromesh.name" $ctx }}
app.kubernetes.io/instance: {{ $ctx.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end }}

{{/* The service's RDF store setting (VEROMESH_RDF_STORE) */}}
{{- define "veromesh.rdfStore" -}}
{{- if .Values.fuseki.enabled -}}
fuseki:http://{{ include "veromesh.fusekiName" . }}:3030/{{ .Values.fuseki.dataset }}
{{- else -}}
{{- required "store.url is required when fuseki.enabled is false" .Values.store.url -}}
{{- end -}}
{{- end }}

{{/* Read replicas, for the service's reads while the writer is unavailable (VEROMESH_RDF_STORE_READ_FALLBACK) */}}
{{- define "veromesh.readFallback" -}}
http://{{ include "veromesh.fusekiReplicasService" . }}:3030/{{ .Values.fuseki.dataset }}
{{- end }}

{{/* Fuseki ping URL for the service's wait-for-store init container */}}
{{- define "veromesh.fusekiPing" -}}
http://{{ include "veromesh.fusekiName" . }}:3030/$/ping
{{- end }}

{{/* Secret holding the DataHub token: the user's, or the one this chart creates */}}
{{- define "veromesh.tokenSecret" -}}
{{- default (printf "%s-datahub" (include "veromesh.fullname" .)) .Values.api.datahub.existingSecret }}
{{- end }}

{{- define "veromesh.tokenSecretKey" -}}
{{- if .Values.api.datahub.existingSecret }}{{ .Values.api.datahub.existingSecretKey }}{{ else }}token{{ end }}
{{- end }}

{{- define "veromesh.apiImage" -}}
{{ .Values.api.image.repository }}:{{ default .Chart.AppVersion .Values.api.image.tag }}
{{- end }}
