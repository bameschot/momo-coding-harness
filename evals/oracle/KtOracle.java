import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import org.jetbrains.kotlin.cli.common.messages.MessageCollector;
import org.jetbrains.kotlin.cli.jvm.compiler.EnvironmentConfigFiles;
import org.jetbrains.kotlin.cli.jvm.compiler.KotlinCoreEnvironment;
import org.jetbrains.kotlin.com.intellij.openapi.Disposable;
import org.jetbrains.kotlin.com.intellij.openapi.util.Disposer;
import org.jetbrains.kotlin.com.intellij.psi.PsiElement;
import org.jetbrains.kotlin.config.CommonConfigurationKeys;
import org.jetbrains.kotlin.config.CompilerConfiguration;
import org.jetbrains.kotlin.lexer.KtTokens;
import org.jetbrains.kotlin.psi.*;

/**
 * Independent Kotlin oracle: the Kotlin 2.2 compiler's own parser (PSI).
 * For each file prints
 *   D <kind> <name> <line>    a declaration (line of its name)
 *   C <name> <line>           a call site (line of the callee name), incl. infix calls
 *   I <fqname> <line>         an import directive
 * No resolution, no type checking: parsing only, like the index.
 */
public class KtOracle {
    static String text;

    static int line(PsiElement e) {
        int off = e.getTextRange().getStartOffset(), n = 1;
        for (int i = 0; i < off && i < text.length(); i++) if (text.charAt(i) == '\n') n++;
        return n;
    }

    static void decl(StringBuilder out, String kind, PsiElement nameId) {
        if (nameId != null) out.append("D\t").append(kind).append('\t').append(nameId.getText())
                .append('\t').append(line(nameId)).append('\n');
    }

    public static void main(String[] args) throws Exception {
        Disposable d = Disposer.newDisposable();
        CompilerConfiguration cfg = new CompilerConfiguration();
        cfg.put(CommonConfigurationKeys.MESSAGE_COLLECTOR_KEY, MessageCollector.Companion.getNONE());
        KotlinCoreEnvironment env = KotlinCoreEnvironment.createForProduction(
                d, cfg, EnvironmentConfigFiles.JVM_CONFIG_FILES);
        KtPsiFactory factory = new KtPsiFactory(env.getProject(), false);
        List<String> files = Files.readAllLines(Path.of(args[0]));
        for (String f : files) {
            text = Files.readString(Path.of(f)).replace("\r\n", "\n");
            KtFile kt = factory.createFile(Path.of(f).getFileName().toString(), text);
            StringBuilder out = new StringBuilder("F\t" + f + "\n");
            kt.accept(new KtTreeVisitorVoid() {
                @Override public void visitNamedFunction(KtNamedFunction fn) {
                    decl(out, "function", fn.getNameIdentifier());
                    super.visitNamedFunction(fn);
                }
                @Override public void visitClassOrObject(KtClassOrObject c) {
                    if (!(c instanceof KtObjectDeclaration o && o.isCompanion() && o.getNameIdentifier() == null))
                        decl(out, c instanceof KtEnumEntry ? "enumentry" : "class", c.getNameIdentifier());
                    super.visitClassOrObject(c);
                }
                @Override public void visitProperty(KtProperty p) {
                    if (!p.isLocal()) decl(out, p.isTopLevel() ? "property" : "member-property", p.getNameIdentifier());
                    super.visitProperty(p);
                }
                @Override public void visitTypeAlias(KtTypeAlias t) {
                    decl(out, "typealias", t.getNameIdentifier());
                    super.visitTypeAlias(t);
                }
                @Override public void visitCallExpression(KtCallExpression call) {
                    KtExpression callee = call.getCalleeExpression();
                    if (callee instanceof KtNameReferenceExpression ref)
                        out.append("C\t").append(ref.getReferencedName()).append('\t').append(line(ref)).append('\n');
                    super.visitCallExpression(call);
                }
                @Override public void visitBinaryExpression(KtBinaryExpression b) {
                    KtOperationReferenceExpression op = b.getOperationReference();
                    if (op.getReferencedNameElementType() == KtTokens.IDENTIFIER)   // infix call: a to b
                        out.append("C\t").append(op.getReferencedName()).append('\t').append(line(op)).append('\n');
                    super.visitBinaryExpression(b);
                }
                @Override public void visitImportDirective(KtImportDirective imp) {
                    if (imp.getImportedFqName() != null)
                        out.append("I\t").append(imp.getImportedFqName().asString())
                           .append(imp.isAllUnder() ? ".*" : "").append('\t').append(line(imp)).append('\n');
                    super.visitImportDirective(imp);
                }
            });
            System.out.print(out);
        }
        Disposer.dispose(d);
    }
}
