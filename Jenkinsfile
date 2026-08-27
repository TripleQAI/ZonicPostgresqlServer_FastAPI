// Embedded pipeline for now — no separate shared-library repo exists yet
// (deployment plan §12.7's "explicitly out of scope for this pass"). Extract to
// tripleq-jenkins-library once a second service needs the same stages.
//
// Stages match deployment plan §12.5 exactly: repeat ci.yml's trusted checks,
// start the approved CodeBuild project, capture the digest, assume
// ZonicEksDeployRole, helm upgrade --atomic, smoke test.

pipeline {
  agent any
  options { timestamps(); disableConcurrentBuilds() }

  environment {
    AWS_REGION           = 'us-east-1'
    ECR_REGISTRY         = '001634074733.dkr.ecr.us-east-1.amazonaws.com'
    ECR_REPOSITORY       = 'zonic-postgresql-server-fastapi'
    CODEBUILD_PROJECT    = 'tripleq-tooling-zonic-postgresql-server'
    CI_ARTIFACT_BUCKET   = 'tripleq-tooling-ci-artifacts-079247879111'
    DEV_DEPLOY_ROLE_ARN  = 'arn:aws:iam::001634074733:role/ZonicEksDeployRole'
    DEV_EKS_CLUSTER      = 'tripleq-development'
    K8S_NAMESPACE        = 'zonic-postgresql-server'
  }

  stages {
    stage('Quality') {
      // Repeats ci.yml's trusted checks on the exact merge commit — GitHub
      // Actions already ran these on the PR, but Jenkins re-runs them on what
      // will actually be built, not what was proposed.
      steps {
        dir('postgresqlmodule/code') {
          // The Jenkins controller's default `python3`/`pip` is 3.9 (AL2023 base) — too
          // old for this project (pyproject.toml/ci.yml both pin 3.12; core/models.py
          // uses PEP 604 `X | Y` union syntax in a module-level type alias, which
          // TypeErrors on 3.9 at import time). python3.12 is installed alongside it
          // specifically for this.
          sh 'python3.12 -m pip install --user -r requirements.txt'
          sh 'ruff check .'
          sh 'ruff format --check .'
          sh 'mypy pgfoundation/core --follow-imports=silent'
          sh 'lint-imports'
          sh 'pytest -q'
        }
      }
    }

    stage('Build · scan · sign (CodeBuild)') {
      when { branch 'main' }
      steps {
        script {
          // CodeBuild's source is S3, not GitHub directly — this avoids CodeBuild
          // needing its own separate GitHub credential. Jenkins zips the commit it
          // already checked out and uploads it; CodeBuild's source-location
          // override points the build at this exact object (deployment plan §9.2:
          // "consumes a source bundle produced by Jenkins").
          def sourceKey = "zonic-postgresql-server-fastapi/source/${env.GIT_COMMIT}.zip"
          sh "zip -r source.zip . -x '.git/*'"
          // The bucket policy explicitly denies any PutObject that doesn't request
          // aws:kms encryption — bucket default encryption alone does not satisfy
          // that condition, since it checks the header on the request itself, not
          // the resulting object. --sse-kms-key-id pins it to the same CMK the
          // Jenkins instance role already has kms:GenerateDataKey/Decrypt on.
          sh "aws s3 cp source.zip s3://${env.CI_ARTIFACT_BUCKET}/${sourceKey} --sse aws:kms --sse-kms-key-id alias/tripleq-tooling-ci-artifacts"

          // Jenkins uses its own EC2 instance role here — CodeBuild lives in the
          // same account (tooling), no cross-account assume-role needed.
          //
          // No --source-version here: for an S3-type source, CodeBuild treats that
          // flag as an S3 object *version ID* (versioning is on for this bucket),
          // not an arbitrary string — passing the git commit SHA there made
          // CodeBuild try to resolve it as a version ID (needing s3:ListBucketVersions,
          // and doomed to fail even with that grant, since it isn't one). Not needed
          // anyway: the object key already embeds the git commit uniquely.
          //
          // Without --source-version, CodeBuild never populates
          // CODEBUILD_RESOLVED_SOURCE_VERSION (it's only set when the source has an
          // actual resolved version) — buildspec.yml would otherwise tag the image
          // "repo:" with an empty tag. Passing the commit through explicitly as
          // IMAGE_TAG instead.
          def buildId = sh(
            script: """
              aws codebuild start-build \
                --project-name ${CODEBUILD_PROJECT} \
                --source-location-override ${env.CI_ARTIFACT_BUCKET}/${sourceKey} \
                --environment-variables-override name=IMAGE_TAG,value=${env.GIT_COMMIT},type=PLAINTEXT \
                --query 'build.id' --output text
            """,
            returnStdout: true
          ).trim()

          def status = 'IN_PROGRESS'
          while (status == 'IN_PROGRESS') {
            sleep(time: 15, unit: 'SECONDS')
            status = sh(
              script: "aws codebuild batch-get-builds --ids ${buildId} --query 'builds[0].buildStatus' --output text",
              returnStdout: true
            ).trim()
          }
          if (status != 'SUCCEEDED') {
            error "CodeBuild ${buildId} finished with status ${status} — see the CodeBuild console for the failing phase (lint/scan findings fail the build, not just the deploy)."
          }

          env.IMAGE_DIGEST = sh(
            script: """
              aws codebuild batch-get-builds --ids ${buildId} \
                --query "builds[0].exportedEnvironmentVariables[?name=='IMAGE_DIGEST'].value | [0]" \
                --output text
            """,
            returnStdout: true
          ).trim()
          echo "Built ${ECR_REGISTRY}/${ECR_REPOSITORY}@${env.IMAGE_DIGEST}"
        }
      }
    }

    stage('Deploy to development') {
      // Automatic on every merge to main — development plays the role a
      // dedicated "staging" environment would (deployment plan §12.2). Production
      // is a separate, explicitly-gated promotion, not part of this pipeline yet.
      when { branch 'main' }
      steps {
        script {
          def creds = sh(
            script: """
              aws sts assume-role \
                --role-arn ${DEV_DEPLOY_ROLE_ARN} \
                --role-session-name jenkins-deploy-development \
                --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
                --output text
            """,
            returnStdout: true
          ).trim().split()

          withEnv([
            "AWS_ACCESS_KEY_ID=${creds[0]}",
            "AWS_SECRET_ACCESS_KEY=${creds[1]}",
            "AWS_SESSION_TOKEN=${creds[2]}",
          ]) {
            sh "aws eks update-kubeconfig --name ${DEV_EKS_CLUSTER} --region ${AWS_REGION}"
            // --atomic: auto-rolls back the release if it doesn't reach ready
            // within --timeout — the Kubernetes-native equivalent of ECS's
            // deployment circuit breaker (deployment plan §12.2). Restored
            // 2026-08-26 after confirming the app reaches Ready (was
            // temporarily removed to preserve failed-pod state for initial
            // deploy debugging — see git history). Also fixes a race this
            // exposed: without --atomic, Deploy returns in ~4s without
            // waiting for the rollout, so Smoke test could hit the ALB while
            // old targets were still Target.DeregistrationInProgress and new
            // ones not yet registered healthy — intermittent 502s with
            // nothing actually wrong.
            sh """
              helm upgrade --install zonic-postgresql-server deploy/helm/zonic-postgresql-server \
                -f deploy/helm/zonic-postgresql-server/values-development.yaml \
                --set image.digest=${env.IMAGE_DIGEST} \
                --namespace ${K8S_NAMESPACE} --create-namespace \
                --atomic --timeout 5m
            """
          }
        }
      }
    }

    stage('Smoke test') {
      when { branch 'main' }
      steps {
        script {
          // The Deploy stage's assumed-role credentials are scoped to its own
          // withEnv block and don't carry over — kubectl's exec-based auth plugin
          // (aws eks get-token) needs valid credentials active in THIS stage too,
          // or it silently falls back to Jenkins' own instance role, which has no
          // EKS access entry on this cluster: "You must be logged in to the
          // server (Unauthorized)". Found in real use, 2026-08-26.
          def creds = sh(
            script: """
              aws sts assume-role \
                --role-arn ${DEV_DEPLOY_ROLE_ARN} \
                --role-session-name jenkins-smoke-test-development \
                --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
                --output text
            """,
            returnStdout: true
          ).trim().split()

          withEnv([
            "AWS_ACCESS_KEY_ID=${creds[0]}",
            "AWS_SECRET_ACCESS_KEY=${creds[1]}",
            "AWS_SESSION_TOKEN=${creds[2]}",
          ]) {
            sh "aws eks update-kubeconfig --name ${DEV_EKS_CLUSTER} --region ${AWS_REGION}"

            // Internal ALB — only reachable from inside the VPC. Jenkins reaches it
            // via the Transit Gateway (bootstrap/IMPORT.md Steps 9-10); the
            // Ingress-created ALB's security group needs to allow tooling's CIDR —
            // see deployment plan §12.5 point 7 if this step fails with a timeout
            // rather than a real HTTP error.
            //
            // --atomic gates Deploy on POD readiness, but the ALB controller's own
            // target-group reconciliation (and, for a first-ever ALB creation e.g.
            // after a scheme change, the ALB's DNS hostname landing in the
            // Ingress's own .status field at all) runs on a separate resync loop
            // that can lag several minutes behind — real observed gaps,
            // 2026-08-26/27 (builds #37, #40): pods were healthy and serving 200s
            // the whole time, but either the target group still held stale IPs
            // from the previous generation, or .status.loadBalancer.ingress[0]
            // .hostname was still empty outright, producing curl failures with
            // nothing actually wrong. Both albHost and ingressHost must be
            // re-fetched INSIDE the retry loop, not once before it — build #40's
            // failure ("curl: (6) Could not resolve host: ready", i.e. an empty
            // albHost baked into the URL) was exactly this: a stale empty value
            // captured once and reused across every retry attempt.
            retry(10) {
              def albHost = sh(
                script: "kubectl get ingress zonic-postgresql-server -n ${K8S_NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'",
                returnStdout: true
              ).trim()
              // The Ingress rule is host-scoped (values-*.yaml's ingress.host) —
              // the ALB's own raw AWS-generated hostname above is just the network
              // address, and curling it directly sends THAT as the Host header,
              // which doesn't match the rule, so the ALB's default action returns
              // a real 404 (not a network error). Fetch the actual configured host
              // and send it explicitly; no DNS record for it needs to exist yet.
              def ingressHost = sh(
                script: "kubectl get ingress zonic-postgresql-server -n ${K8S_NAMESPACE} -o jsonpath='{.spec.rules[0].host}'",
                returnStdout: true
              ).trim()
              if (!albHost) {
                sh "echo 'ALB hostname not yet assigned to Ingress status — retrying' && sleep 15 && exit 1"
              }
              sh "curl -fsS --max-time 10 -H \"Host: ${ingressHost}\" http://${albHost}/ready || (sleep 15 && exit 1)"
            }
          }
        }
      }
    }
  }

  post {
    failure {
      echo 'Pipeline failed. If the deploy stage was reached, helm --atomic already rolled back any partial release — no manual cleanup needed.'
    }
  }
}
