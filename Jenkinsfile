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
          sh 'pip install -r requirements.txt'
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
          sh "aws s3 cp source.zip s3://${env.CI_ARTIFACT_BUCKET}/${sourceKey}"

          // Jenkins uses its own EC2 instance role here — CodeBuild lives in the
          // same account (tooling), no cross-account assume-role needed.
          def buildId = sh(
            script: """
              aws codebuild start-build \
                --project-name ${CODEBUILD_PROJECT} \
                --source-location-override s3://${env.CI_ARTIFACT_BUCKET}/${sourceKey} \
                --source-version ${env.GIT_COMMIT} \
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
            // deployment circuit breaker (deployment plan §12.2).
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
          // Internal ALB — only reachable from inside the VPC. Jenkins reaches it
          // via the Transit Gateway (bootstrap/IMPORT.md Steps 9-10); the
          // Ingress-created ALB's security group needs to allow tooling's CIDR —
          // see deployment plan §12.5 point 7 if this step fails with a timeout
          // rather than a real HTTP error.
          def host = sh(
            script: "kubectl get ingress zonic-postgresql-server -n ${K8S_NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'",
            returnStdout: true
          ).trim()
          sh "curl -fsS --max-time 10 http://${host}/ready"
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
