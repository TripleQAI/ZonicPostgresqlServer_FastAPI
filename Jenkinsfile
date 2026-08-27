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
    // Matches values-development.yaml's ingress.host — kept as a literal here
    // rather than read from the chart, same as this block's other environment-
    // specific constants (DEV_EKS_CLUSTER etc.).
    PUBLIC_API_HOST      = 'pgfoundation.dev.tripleq.ai'
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
            // --timeout 8m (was 5m) — root-caused 2026-08-27: the
            // secrets-store-csi-driver-provider-aws pods occasionally hit a
            // transient DNS timeout reaching Secrets Manager ("dial tcp: lookup
            // secretsmanager.us-east-1.amazonaws.com: i/o timeout"), seen on
            // BOTH provider pods across both nodes/AZs — not a misconfiguration
            // (the VPC endpoint was healthy/available, CoreDNS had zero errors
            // or restarts), genuine transient network flakiness. kubelet's own
            // FailedMount retry loop already recovers on its own within roughly
            // 1-3 minutes; the problem was purely that 5m wasn't enough
            // headroom to absorb that PLUS two sequential pod readiness cycles
            // (maxUnavailable: 0) without tripping the rollback. This is the
            // real cause of builds #37/#39/#41/#43's "Pending termination: 1"
            // failures — see scripts/aws-commands.ps1 in the infra repo for
            // the full diagnosis (provider/CoreDNS/VPC-endpoint logs).
            sh """
              helm upgrade --install zonic-postgresql-server deploy/helm/zonic-postgresql-server \
                -f deploy/helm/zonic-postgresql-server/values-development.yaml \
                --set image.digest=${env.IMAGE_DIGEST} \
                --namespace ${K8S_NAMESPACE} --create-namespace \
                --atomic --timeout 8m
            """
          }
        }
      }
    }

    stage('Smoke test') {
      when { branch 'main' }
      steps {
        // Simplified 2026-08-27 once development's Ingress got a real public
        // hostname (pgfoundation.dev.tripleq.ai, internet-facing ALB with a
        // publicly-trusted ACM cert). No more kubectl/assume-role dance to look
        // up the ALB's raw hostname, no Host-header override, no HTTP-then-
        // redirect hop: curl the real hostname over HTTPS directly, exactly how
        // a real caller would reach it. This also sidesteps a real bug found in
        // the old approach — Jenkins' security group only had port-80 egress
        // scoped to the OLD internal ALB's private VPC CIDR, which doesn't cover
        // this ALB's public IPs at all; HTTPS reuses the already-broad
        // `443 -> 0.0.0.0/0` egress rule Jenkins already had, so no SG change
        // was needed.
        //
        // Still retried, not one-shot: the AWS Load Balancer Controller's target-
        // group reconciliation runs on its own resync loop that can lag a couple
        // minutes behind pod readiness — real observed gap, 2026-08-26/27
        // (builds #37, #40): pods were healthy and serving 200s the whole time,
        // but the target group briefly held stale/draining targets right after a
        // deploy, producing curl failures with nothing actually wrong.
        retry(10) {
          sh "curl -fsS --max-time 10 https://${PUBLIC_API_HOST}/ready || (sleep 15 && exit 1)"
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
