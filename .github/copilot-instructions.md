# GitHub Copilot Instructions for Homelab Infrastructure

## Repository Overview

This repository manages a GitOps-based homelab infrastructure primarily using Kubernetes. All infrastructure changes are made through declarative configuration files stored in Git, following GitOps principles.

## Core Principles

### GitOps Workflow
- **Git as Single Source of Truth**: All infrastructure configuration is version-controlled in Git
- **Declarative Configuration**: Use declarative YAML manifests for all Kubernetes resources
- **Automated Synchronization**: Changes pushed to Git trigger automated deployment workflows
- **Pull-based Deployment**: ArgoCD pulls configuration from Git to deploy to Kubernetes clusters
- **Audit Trail**: All changes are tracked through Git history with meaningful commit messages

### Infrastructure as Code
- All infrastructure components are defined as code
- Configuration changes go through code review via Pull Requests
- Use descriptive commit messages that explain the "why" not just the "what"
- Never manually modify cluster resources - always update the source files in Git

## Technology Stack

### Kubernetes Ecosystem
- **Kubernetes**: Container orchestration platform
- **ArgoCD**: GitOps continuous delivery tool for Kubernetes
- **CloudNativePG**: PostgreSQL operator for cloud-native database management
- **kubectl**: Kubernetes CLI for cluster management
- **Helm**: Package manager for Kubernetes (values.yaml files)

### Automation & CI/CD
- **GitHub Actions**: CI/CD pipelines for automation workflows
- **Renovate**: Automated dependency updates with auto-merge policies
- **Tailscale**: Secure mesh networking for private cluster access

### Security & Secrets
- **Vault**: HashiCorp Vault for centralized secrets management
- **KUBECONFIG**: Stored as GitHub secrets, base64-encoded

### Monitoring & Health Checks
- Automated health checks for Kubernetes nodes, PostgreSQL replication, and backups
- Email notifications for failures
- Regular validation of WAL archiving and backup schedules

## File Organization

### Kubernetes Manifests
- ArgoCD application manifests: `kubernetes/apps/*.yaml`
- Helm values files: `**/values.yaml`
- Database configurations: `kubernetes/cloudnative-pg/databases/*.yaml`
- Deployment manifests: `**/deployment.yaml` or `**/deployment.yml`

### GitHub Actions Workflows
- All workflows in `.github/workflows/*.yml`
- Use descriptive workflow names
- Include `workflow_dispatch` for manual triggering
- Use concurrency controls to prevent conflicting runs

### Scripts
- Bash scripts in `scripts/` directory
- Follow naming convention: `<purpose>_<schedule>_script.sh`

## Best Practices

### Kubernetes Manifests

#### General Guidelines
- Use explicit API versions (avoid deprecated APIs)
- Always specify resource limits and requests
- Use meaningful labels and annotations
- Include namespace specifications
- Use `---` separator between multiple resources in one file

#### Resource Management
```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

#### Labels and Selectors
- Use consistent labeling: `app`, `component`, `version`, `managed-by`
- Example:
```yaml
metadata:
  labels:
    app: myapp
    component: backend
    managed-by: argocd
```

#### Security Context
- Always define security context for pods
- Run as non-root when possible
- Use read-only root filesystem where applicable

### ArgoCD Applications

#### Application Manifest Structure
- Place ArgoCD applications in `kubernetes/apps/` directory
- Use declarative Application CRDs
- Specify sync policies (automated vs manual)
- Configure pruning and self-healing policies

#### Sync Configuration
```yaml
spec:
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### CloudNativePG Databases

#### Database Cluster Configuration
- Always configure backup schedules
- Enable WAL archiving for point-in-time recovery
- Use CloudNativePG's ScheduledBackup resources
- Monitor backup health with automated checks

#### Connection Management
- Store database credentials in Vault
- Use Kubernetes secrets synced from Vault
- Never hardcode database passwords

### Renovate Configuration

#### Dependency Management
- Renovate automatically updates images, Helm charts, and GitHub Actions
- Use package rules to control auto-merge behavior
- Critical components (NGINX, databases) should never auto-merge
- Major version updates require manual review

#### Adding Custom Regex Managers
```json
"customManagers": [
  {
    "customType": "regex",
    "fileMatch": ["kubernetes/.*\\.yaml$"],
    "matchStrings": ["imageName:\\s*(?<depName>[^:]+):(?<currentValue>.+?)\\n"],
    "datasourceTemplate": "docker"
  }
]
```

### GitHub Actions Workflows

#### Workflow Structure
- Include clear job names and step descriptions
- Use timeouts to prevent hanging workflows
- Set minimal permissions (`permissions:` block)
- Use concurrency groups to prevent conflicts

#### Kubernetes Access Pattern
```yaml
- name: Connect to Tailscale
  uses: tailscale/github-action@v4
  with:
    oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
    oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}

- name: Setup kubectl
  uses: azure/setup-kubectl@v4

- name: Set up kubeconfig
  run: |
    mkdir -p $HOME/.kube
    base64 -d > $HOME/.kube/config << 'EOF'
    ${{ secrets.KUBECONFIG }}
    EOF
    chmod 600 $HOME/.kube/config
```

#### Secret Handling
- Never log secrets or sensitive output
- Use GitHub secrets for all credentials
- Clear secrets from disk in cleanup steps
- Use `if: always()` for cleanup steps

### Security Best Practices

#### Secrets Management
- Store all secrets in HashiCorp Vault
- Sync secrets to Kubernetes using automated workflows
- Never commit secrets to Git
- Use base64 encoding for kubeconfig and other credentials

#### Network Security
- Use Tailscale for secure private network access
- Restrict ingress to necessary endpoints only
- Use network policies in Kubernetes
- Tag CI runners appropriately (e.g., `tag:ci`)

#### Image Security
- Use specific image tags, never `latest`
- Pull images from trusted registries
- Use registry mirrors/proxies for rate limiting and caching
- Example: `harbor.mfaherty.net/dockerhub-proxy` for Docker Hub

### Monitoring and Alerting

#### Health Checks
- Implement regular health checks for critical components
- Check Kubernetes node readiness
- Monitor PostgreSQL replication lag
- Verify backup schedules and success
- Validate WAL archiving status

#### Alerting
- Send email notifications for failures
- Use descriptive subject lines with emoji indicators (✅/❌)
- Include workflow run links in alert emails
- Consider severity levels for different alert types

### Docker and Container Best Practices

#### Image Management
- Use multi-stage builds for smaller images
- Pin base image versions
- Scan images for vulnerabilities
- Use distroless or minimal base images when possible

#### Resource Optimization
- Set appropriate resource limits
- Use horizontal pod autoscaling where beneficial
- Configure pod disruption budgets for HA applications

### Database Management

#### CloudNativePG Best Practices
- Configure scheduled backups (at least daily)
- Test backup restoration procedures
- Monitor WAL archiving continuously
- Use appropriate PostgreSQL versions
- Configure replication for high availability
- Set backup retention policies

#### Backup Validation
- Verify backup age (alert if > 48 hours old)
- Check WAL continuous archiving status
- Monitor replication lag
- Validate backup completion events

### Git Commit Guidelines

#### Commit Messages
- Use conventional commit format when appropriate
- Examples:
  - `feat: add new database backup workflow`
  - `fix: correct NGINX ingress configuration`
  - `chore: update CloudNativePG image version`
  - `docs: update copilot instructions`

#### Pull Request Guidelines
- Keep PRs focused and atomic
- Include context in PR descriptions
- Link to related issues
- Wait for automated checks before merging
- Review Renovate PRs carefully for critical components

### Renovate Auto-Merge Rules

#### Components That NEVER Auto-Merge
- NGINX ingress controller (requires manual review with `needs-nginx-review` label)
- CloudNativePG database images (requires manual review with `needs-db-review` label)
- Major version updates (breaking changes possible)

#### Components That Can Auto-Merge
- Minor and patch updates (after validation)
- GitHub Actions updates
- Non-critical dependency updates
- Changes that pass all automated tests

### Homelab-Specific Considerations

#### Infrastructure Components
- Proxmox virtualization hosts
- Pi-hole DNS servers
- Docker hosts for containerized services
- Unbound recursive DNS
- NFS storage
- Linode backup services

#### Maintenance Windows
- Use scheduled workflows for regular maintenance
- Coordinate restart operations to minimize downtime
- Schedule backups during low-usage periods
- Use kured for node reboots with proper draining

#### Network Architecture
- Tailscale mesh network for secure access
- Pi-hole for network-wide ad blocking and DNS
- Unbound for recursive DNS resolution
- Private registry mirrors for image caching

## Common Operations

### Adding a New Kubernetes Application
1. Create ArgoCD Application manifest in `kubernetes/apps/`
2. Define application source repository and path
3. Configure sync policy (automated or manual)
4. Set destination namespace
5. Add any necessary Renovate rules for the application
6. Submit PR and wait for approval

### Updating Container Images
1. Let Renovate automatically detect and create PRs
2. Review the PR for breaking changes
3. Check if component requires manual review (NGINX, databases)
4. Verify CI checks pass
5. Merge if appropriate, or test in staging first

### Adding a New Workflow
1. Create workflow file in `.github/workflows/`
2. Include workflow_dispatch for manual testing
3. Set appropriate permissions
4. Use Tailscale for private network access
5. Include error handling and notifications
6. Add cleanup steps with `if: always()`

### Managing Secrets
1. Store secret in HashiCorp Vault
2. Update vault-sync-secrets workflow if needed
3. Reference secret in Kubernetes manifests as needed
4. Never commit plaintext secrets to Git

## Troubleshooting

### Workflow Failures
- Check GitHub Actions logs for error messages
- Verify Tailscale connectivity
- Confirm kubeconfig is valid
- Check secret expiration dates
- Review concurrency conflicts

### ArgoCD Sync Issues
- Verify Git repository accessibility
- Check for syntax errors in manifests
- Review ArgoCD application health status
- Confirm namespace exists
- Check RBAC permissions

### Database Issues
- Check CloudNativePG cluster status
- Verify WAL archiving is working
- Confirm backup schedules are running
- Review replication lag metrics
- Check storage capacity

## Additional Resources

### Relevant Documentation
- Kubernetes Official Docs: https://kubernetes.io/docs/
- ArgoCD Documentation: https://argo-cd.readthedocs.io/
- CloudNativePG: https://cloudnative-pg.io/
- Renovate Docs: https://docs.renovatebot.com/
- Tailscale: https://tailscale.com/kb/

### Renovate Manager Types in Use
- `docker-compose`: Docker Compose files
- `helm-values`: Helm values.yaml files
- `kubernetes`: Kubernetes deployment manifests
- `argocd`: ArgoCD application manifests
- `github-actions`: GitHub Actions workflow files

## Code Generation Guidelines

When generating code for this repository:

1. **Follow Existing Patterns**: Review similar files before creating new ones
2. **Use Consistent Naming**: Match naming conventions for workflows, manifests, and scripts
3. **Include Required Fields**: Always include necessary metadata, labels, and annotations
4. **Security First**: Never expose secrets, always use proper authentication
5. **Add Monitoring**: Include health checks and alerting for new services
6. **Document Changes**: Add comments for complex logic, update README if needed
7. **Test Before Commit**: Validate YAML syntax, test workflows with workflow_dispatch
8. **Consider Dependencies**: Update Renovate config for new dependencies
9. **Think GitOps**: Ensure changes are declarative and can be applied repeatedly
10. **Plan for Failures**: Include error handling, timeouts, and cleanup steps
