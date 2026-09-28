/** Lists C++ translation units under the analyzed source root. */
import cpp

from File file
where file.fromSource() and file.compiledAsCpp()
select file.getRelativePath() as path
